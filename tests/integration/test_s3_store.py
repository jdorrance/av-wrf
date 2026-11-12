"""S3 SDK conditional writes, credentials, and error handling."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO, StringIO
from unittest.mock import patch

import boto3
import pytest
from botocore.config import Config
from botocore.credentials import Credentials
from botocore.exceptions import ClientError, IncompleteReadError, ReadTimeoutError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from av_wrf.runner.s3 import CASConflict, S3Config, S3Store
from av_wrf.runner.store import put_idempotent

_CONFIG = S3Config("https://storage.example/base", "auto", "access", "secret", "session")


def test_client_configuration_and_credential_chain() -> None:
    with patch("av_wrf.runner.s3.boto3.client", wraps=boto3.client) as factory:
        store = S3Store("s3://bucket", _CONFIG)
    kwargs = factory.call_args.kwargs
    assert kwargs["endpoint_url"] == _CONFIG.endpoint
    assert kwargs["region_name"] == "auto"
    assert kwargs["aws_access_key_id"] == "access"
    assert kwargs["aws_secret_access_key"] == "secret"
    assert kwargs["aws_session_token"] == "session"
    assert store._client.meta.endpoint_url == _CONFIG.endpoint
    assert store._client.meta.config.__dict__["proxies"] == {}
    assert store._client.meta.config.__dict__["s3"] == {"addressing_style": "path"}
    assert store._client.meta.config.__dict__["request_checksum_calculation"] == "when_required"
    assert store._client.meta.config.__dict__["response_checksum_validation"] == "when_required"
    assert store._client.meta.config.__dict__["retries"] == {
        "mode": "standard",
        "total_max_attempts": 3,
    }
    assert "secret" not in repr(_CONFIG) and "session" not in repr(_CONFIG)
    store.close()
    with patch(
        "botocore.session.Session.get_credentials", return_value=Credentials("role", "secret")
    ) as provider:
        with patch("av_wrf.runner.s3.boto3.client", wraps=boto3.client) as factory:
            store = S3Store("s3://bucket", S3Config("https://storage.example", "us-east-1", "", ""))
        assert factory.call_args.kwargs["aws_access_key_id"] is None
        assert factory.call_args.kwargs["aws_secret_access_key"] is None
        assert factory.call_args.kwargs["aws_session_token"] is None
        provider.assert_called()
        store.close()


def test_conditional_writes_and_opaque_tokens() -> None:
    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
    )
    store = S3Store("s3://bucket/prefix", _CONFIG, client=client)
    with Stubber(client) as stub:
        stub.add_response(
            "put_object",
            {"ETag": '"initial-token"'},
            {"Bucket": "bucket", "Key": "prefix/latest", "Body": b"initial", "IfNoneMatch": "*"},
        )
        stub.add_response(
            "put_object",
            {"ETag": '"next-token"'},
            {
                "Bucket": "bucket",
                "Key": "prefix/latest",
                "Body": b"next",
                "IfMatch": '"initial-token"',
            },
        )
        stub.add_response(
            "put_object",
            {},
            {
                "Bucket": "bucket",
                "Key": "prefix/unicode/space key",
                "Body": b"hello",
                "IfNoneMatch": "*",
            },
        )
        assert store.compare_and_swap("latest", b"initial", None) == '"initial-token"'
        assert store.compare_and_swap("latest", b"next", '"initial-token"') == '"next-token"'
        assert (
            store.put_if_absent("unicode/space key", b"hello")
            == hashlib.sha256(b"hello").hexdigest()
        )
        stub.assert_no_pending_responses()
    store.close()


@pytest.mark.parametrize("status", [409, 412])
def test_conflicts_and_idempotent_create(status: int) -> None:
    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
    )
    store = S3Store("s3://bucket", _CONFIG, client=client)
    params = {"Bucket": "bucket", "Key": "key", "Body": b"value", "IfNoneMatch": "*"}
    stream = BytesIO(b"value")
    with Stubber(client) as stub:
        for _ in range(2):
            stub.add_client_error(
                "put_object", "PreconditionFailed", http_status_code=status, expected_params=params
            )
        stub.add_response(
            "get_object",
            {"Body": StreamingBody(stream, 5), "ETag": '"etag"'},
            {"Bucket": "bucket", "Key": "key"},
        )
        stub.add_client_error(
            "put_object",
            "PreconditionFailed",
            http_status_code=status,
            expected_params={
                "Bucket": "bucket",
                "Key": "key",
                "Body": b"value",
                "IfMatch": '"old"',
            },
        )
        with pytest.raises(CASConflict):
            store.compare_and_swap("key", b"value", None)
        assert put_idempotent(store, "key", b"value") == hashlib.sha256(b"value").hexdigest()
        assert stream.closed
        with pytest.raises(CASConflict):
            store.compare_and_swap("key", b"value", '"old"')
        stub.assert_no_pending_responses()
    store.close()


def test_reads_close_body_and_missing_etag_fails() -> None:
    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
    )
    store = S3Store("s3://bucket", _CONFIG, client=client)
    good, missing, truncated = BytesIO(b"payload"), BytesIO(b"missing"), BytesIO(b"short")
    with Stubber(client) as stub:
        stub.add_response(
            "get_object",
            {"Body": StreamingBody(good, 7), "ETag": '"opaque-2"'},
            {"Bucket": "bucket", "Key": "key"},
        )
        stub.add_response(
            "get_object", {"Body": StreamingBody(missing, 7)}, {"Bucket": "bucket", "Key": "key"}
        )
        stub.add_response(
            "put_object", {}, {"Bucket": "bucket", "Key": "key", "Body": b"x", "IfNoneMatch": "*"}
        )
        stub.add_response(
            "get_object",
            {"Body": StreamingBody(truncated, 10), "ETag": '"etag"'},
            {"Bucket": "bucket", "Key": "key"},
        )
        assert store.get_with_token("key") == (b"payload", '"opaque-2"')
        with pytest.raises(ValueError, match="ETag"):
            store.get_bytes("key")
        with pytest.raises(ValueError, match="ETag"):
            store.compare_and_swap("key", b"x", None)
        with pytest.raises(IncompleteReadError):
            store.get_bytes("key")
        assert good.closed and missing.closed and truncated.closed
        stub.assert_no_pending_responses()
    store.close()


def test_cas_adopts_committed_value_after_response_timeout() -> None:
    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
    )
    store = S3Store("s3://bucket", _CONFIG, client=client)
    with (
        patch.object(client, "put_object", side_effect=ReadTimeoutError(endpoint_url="test")),
        Stubber(client) as stub,
    ):
        for value in (b"claim", b"other"):
            stub.add_response("get_object", {"Body": StreamingBody(BytesIO(value), 5), "ETag": "t"})
        assert store.compare_and_swap("key", b"claim", None) == "t"
        with pytest.raises(ReadTimeoutError):
            store.compare_and_swap("key", b"claim", None)
    store.close()


@pytest.mark.parametrize("code", ["NoSuchBucket", "404", "AccessDenied"])
def test_only_no_such_key_is_missing(code: str) -> None:
    client = boto3.client(
        "s3", region_name="us-east-1", aws_access_key_id="a", aws_secret_access_key="b"
    )
    store = S3Store("s3://bucket", _CONFIG, client=client)
    with Stubber(client) as stub:
        for error in ("NoSuchKey", "NoSuchKey", code):
            stub.add_client_error(
                "get_object",
                error,
                http_status_code=404,
                expected_params={"Bucket": "bucket", "Key": "key"},
            )
        assert store.get_with_token("key") == (None, None)
        with pytest.raises(FileNotFoundError):
            store.ensure_durable("key")
        with pytest.raises(ClientError):
            store.get_bytes("key")
        stub.assert_no_pending_responses()
    store.close()


def test_cloud_delete_endpoint_credentials_and_keys() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        S3Store("s3://bucket", S3Config("http://storage.example", "auto", "a", "b"))
    with pytest.raises(ValueError, match="credential pair"):
        S3Store("s3://bucket", S3Config("https://storage.example", "auto", "a", ""))
    with pytest.raises(ValueError, match="session token"):
        S3Store("s3://bucket", S3Config("https://storage.example", "auto", "", "", "token"))
    with pytest.raises(ValueError, match="prefix"):
        S3Store("s3://bucket", S3Config("https://storage.example/a/../b", "auto", "a", "b"))
    store = S3Store("s3://bucket", S3Config("http://localhost:9000", "auto", "a", "b"))
    with pytest.raises(ValueError, match="unsafe"):
        store.delete_prefix("stage")
    for key in ("", "/key", "key//part", "key/../part"):
        with pytest.raises(ValueError, match="key"):
            store.get_bytes(key)
    with store.cycle_lock("cycle"):
        pass
    store.close()


_TOKEN = "sentinel-session-token-private"
_SECRET = "sentinel-temporary-secret-private"
_ROLE_RESPONSE = f"""<AssumeRoleResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
<AssumeRoleResult><Credentials><AccessKeyId>temporary-access-key</AccessKeyId>
<SecretAccessKey>{_SECRET}</SecretAccessKey><SessionToken>{_TOKEN}</SessionToken>
<Expiration>2035-01-01T00:00:00Z</Expiration></Credentials></AssumeRoleResult>
<ResponseMetadata><RequestId>request-id</RequestId></ResponseMetadata></AssumeRoleResponse>"""


class _Server(ThreadingHTTPServer):
    requests: list[tuple[str, dict[str, str]]]


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length", "0")))
        self._respond(_ROLE_RESPONSE.encode())

    def do_GET(self) -> None:
        self._respond(b"payload")

    def _respond(self, body: bytes) -> None:
        assert isinstance(self.server, _Server)
        self.server.requests.append((self.path, dict(self.headers.items())))
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"etag"')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextlib.contextmanager
def _server() -> Iterator[_Server]:
    server = _Server(("127.0.0.1", 0), _Handler)
    server.requests = []
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


def _url(server: _Server) -> str:
    return f"http://127.0.0.1:{server.server_port}"


def test_signed_requests_and_role_credentials_do_not_reach_debug_handlers(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    for name in ("boto3", "botocore", "s3transfer"):
        logging.getLogger(name)
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if isinstance(logger, logging.Logger) and name.split(".")[0] in {
            "boto3",
            "botocore",
            "s3transfer",
        }:
            monkeypatch.setattr(logger, "handlers", logger.handlers.copy())
            monkeypatch.setattr(logger, "propagate", logger.propagate)
    output = StringIO()
    for name in ("botocore.auth", "botocore.endpoint", "botocore.parsers"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "level", logging.DEBUG)
        monkeypatch.setattr(logger, "handlers", [logging.StreamHandler(output)])
        monkeypatch.setattr(logger, "propagate", False)
    with _server() as server, _server() as proxy:
        monkeypatch.setenv("HTTP_PROXY", _url(proxy))
        monkeypatch.setenv("http_proxy", _url(proxy))
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        sts = boto3.client(
            "sts",
            endpoint_url=_url(server),
            region_name="us-east-1",
            aws_access_key_id="source-access",
            aws_secret_access_key="source-secret",
            config=Config(proxies={}),
        )

        def role_credentials() -> Credentials:
            response = sts.assume_role(
                RoleArn="arn:aws:iam::123456789012:role/test", RoleSessionName="test"
            )
            credentials = response["Credentials"]
            return Credentials(
                credentials["AccessKeyId"],
                credentials["SecretAccessKey"],
                credentials["SessionToken"],
            )

        caplog.clear()
        with patch("botocore.session.Session.get_credentials", side_effect=role_credentials):
            store = S3Store("s3://bucket", S3Config(_url(server), "us-east-1", "", ""))
            assert store._client.meta.config.__dict__["proxies"] == {}
            assert store.get_bytes("key") == b"payload"
            store.close()
        sts.close()
        assert len(server.requests) == 2
        assert server.requests[1][1]["X-Amz-Security-Token"] == _TOKEN
        assert server.requests[1][1]["Authorization"].startswith("AWS4-HMAC-SHA256")
        assert proxy.requests == []
    logging.getLogger("application").debug("application debug is visible")
    logging.getLogger("botocore.new_child").debug(_TOKEN)
    assert "application debug is visible" in caplog.text
    for sensitive in (_TOKEN, _SECRET, "AWS4-HMAC-SHA256"):
        assert sensitive not in caplog.text
        assert sensitive not in output.getvalue()
