"""Conditional S3-compatible object operations using boto3."""

from __future__ import annotations

import contextlib
import hashlib
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError, HTTPClientError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


class CASConflict(RuntimeError):
    """The current object token differs from the expected token."""


@dataclass(frozen=True)
class S3Config:
    endpoint: str
    region: str
    access_key: str = field(repr=False)
    secret_key: str = field(repr=False)
    session_token: str = field(default="", repr=False)


class S3Store:
    """Path-style S3/R2 storage with conditional puts and a shared connection pool."""

    def __init__(self, uri: str, config: S3Config, *, client: S3Client | None = None) -> None:
        parsed, endpoint = urlsplit(uri), urlsplit(config.endpoint)
        if (
            parsed.scheme != "s3"
            or not parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.port
        ):
            raise ValueError("Expected s3://bucket/optional-prefix")
        if (
            endpoint.scheme not in ("http", "https")
            or not endpoint.netloc
            or endpoint.query
            or endpoint.fragment
            or endpoint.username
            or endpoint.password
        ):
            raise ValueError("Expected an S3 HTTP(S) endpoint without credentials or query")
        if endpoint.scheme == "http" and endpoint.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("S3 endpoint must use HTTPS except on loopback")
        if not config.region or bool(config.access_key) != bool(config.secret_key):
            raise ValueError("S3 region and a complete credential pair are required")
        if config.session_token and not config.access_key:
            raise ValueError("S3 session token requires explicit credentials")
        self._bucket = parsed.netloc
        self._prefix = parsed.path.strip("/")
        paths = (endpoint.path.strip("/"), self._prefix)
        if any(part in ("", ".", "..") for path in paths if path for part in path.split("/")):
            raise ValueError("Invalid S3 bucket or prefix")
        # SDK DEBUG records can contain signed headers and credential response bodies.
        for name in ("boto3", "botocore", "s3transfer"):
            namespace = logging.getLogger(name)
            for child_name, child in list(logging.Logger.manager.loggerDict.items()):
                if child_name.startswith(name + ".") and isinstance(child, logging.Logger):
                    child.handlers = []
                    child.propagate = True
            namespace.handlers = [logging.NullHandler()]
            namespace.propagate = False
        self._client = client or boto3.client(
            "s3",
            endpoint_url=config.endpoint,
            region_name=config.region,
            aws_access_key_id=config.access_key or None,
            aws_secret_access_key=config.secret_key or None,
            aws_session_token=config.session_token or None,
            config=Config(
                proxies={},
                s3={"addressing_style": "path"},
                # R2 supports composite CRC32. The SDK's default PUT trailer is full-object.
                # https://developers.cloudflare.com/r2/api/s3/api/#checksum-types
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
                retries={"mode": "standard", "total_max_attempts": 3},
                connect_timeout=10,
                read_timeout=60,
                max_pool_connections=8,
            ),
        )

    def _key(self, key: str) -> str:
        if not key or any(part in ("", ".", "..") for part in key.split("/")):
            raise ValueError("Invalid S3 object key")
        return "/".join(part for part in (self._prefix, key) if part)

    def get_with_token(self, key: str) -> tuple[bytes | None, str | None]:
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=self._key(key))
        except ClientError as exc:
            # AWS requires ListBucket permission to distinguish missing keys from AccessDenied.
            # https://docs.aws.amazon.com/AmazonS3/latest/API/API_GetObject.html
            if exc.response.get("Error", {}).get("Code") == "NoSuchKey":
                return None, None
            raise
        body = response["Body"]
        try:
            payload = body.read()
        finally:
            body.close()
        token = response.get("ETag")
        if not token:
            raise ValueError("S3 response lacks an ETag")
        return payload, token

    def get_bytes(self, key: str) -> bytes | None:
        return self.get_with_token(key)[0]

    def ensure_durable(self, key: str) -> None:
        """Confirm the remote object exists; successful S3 writes are durable."""
        if self.get_bytes(key) is None:
            raise FileNotFoundError(key)

    def put_if_absent(self, key: str, payload: bytes) -> str:
        try:
            self._client.put_object(
                Bucket=self._bucket, Key=self._key(key), Body=payload, IfNoneMatch="*"
            )
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in (409, 412):
                raise FileExistsError(key) from exc
            raise
        return hashlib.sha256(payload).hexdigest()

    def compare_and_swap(self, key: str, payload: bytes, expected_token: str | None) -> str:
        try:
            if expected_token is None:
                response = self._client.put_object(
                    Bucket=self._bucket, Key=self._key(key), Body=payload, IfNoneMatch="*"
                )
            else:
                response = self._client.put_object(
                    Bucket=self._bucket, Key=self._key(key), Body=payload, IfMatch=expected_token
                )
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") in (409, 412):
                raise CASConflict(f"CAS conflict on {key}") from exc
            raise
        except HTTPClientError:
            # A lost response can leave a committed state even after SDK retries fail.
            current, token = self.get_with_token(key)
            if current == payload and token is not None:
                return token
            raise
        token = response.get("ETag")
        if not token:
            raise ValueError("S3 response lacks an ETag")
        return token

    def delete_prefix(self, prefix: str) -> None:
        raise ValueError("Cloud deletion is unsafe while writers may be paused")

    @contextlib.contextmanager
    def cycle_lock(self, prefix: str) -> Iterator[None]:
        # Cloud writers fence commits with object preconditions.
        yield

    def close(self) -> None:
        """Close the connection pool when the store is no longer used."""
        self._client.close()


__all__ = ["CASConflict", "S3Config", "S3Store"]
