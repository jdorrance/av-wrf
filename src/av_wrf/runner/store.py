"""ObjectStore protocol and flock-serialized atomic local storage implementation."""

from __future__ import annotations

import fcntl
import hashlib
import uuid
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


class ObjectStore(Protocol):
    """Minimal immutable-put and compare-and-swap object storage boundary."""

    def get_bytes(self, key: str) -> bytes | None: ...
    def put_if_absent(self, key: str, payload: bytes) -> str: ...
    def compare_and_swap(
        self, key: str, payload: bytes, expected_sha256: str | None
    ) -> str: ...


class LocalFileStore:
    """Filesystem ObjectStore enforcing exclusive puts and flock-serialized CAS."""

    def __init__(self, root_dir: Path) -> None:
        self._root = root_dir.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _resolve(self, key: str) -> Path:
        clean = key.lstrip("/")
        if not clean:
            raise ValueError("Store key must be non-empty")
        target = (self._root / clean).resolve()
        if self._root not in target.parents:
            raise ValueError(f"Store key escapes root directory: {key}")
        return target

    def get_bytes(self, key: str) -> bytes | None:
        path = self._resolve(key)
        return path.read_bytes() if path.is_file() else None

    def put_if_absent(self, key: str, payload: bytes) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as handle:
                handle.write(payload)
        except FileExistsError as exc:
            raise FileExistsError(f"Immutable object already exists: {key}") from exc
        return _sha256_hex(payload)

    def compare_and_swap(
        self, key: str, payload: bytes, expected_sha256: str | None
    ) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.parent / f".{path.name}.cas.lock"
        with lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                current = path.read_bytes() if path.is_file() else None
                actual = _sha256_hex(current) if current is not None else None
                if actual != expected_sha256:
                    raise RuntimeError(
                        f"CAS conflict on {key}: expected {expected_sha256}, found {actual}"
                    )
                tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
                tmp.write_bytes(payload)
                tmp.replace(path)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        return _sha256_hex(payload)


def open_store(store_uri: str) -> ObjectStore:
    """Open an ObjectStore from a file:// URI (S3/R2 obstore wiring lands in Phase 2)."""
    parsed = urlparse(store_uri)
    if parsed.scheme in ("", "file"):
        raw_path = parsed.path if parsed.scheme == "file" else store_uri
        return LocalFileStore(Path(raw_path))
    raise ValueError(
        f"Unsupported scheme '{parsed.scheme}://' in Phase 1; use file:// until Phase 2"
    )
