"""ObjectStore protocol, key builders, streaming hashes, exclusive cycle lock, and CAS."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import hashlib
import json
import os
import shutil
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse
from weakref import WeakValueDictionary

_INPROC_LOCKS_GUARD = threading.Lock()
_INPROC_LOCKS: WeakValueDictionary[Path, threading.Lock] = WeakValueDictionary()


def _inproc_lock_for(path: Path) -> threading.Lock:
    with _INPROC_LOCKS_GUARD:
        lk = _INPROC_LOCKS.get(path)
        if lk is None:
            lk = threading.Lock()
            _INPROC_LOCKS[path] = lk
        return lk


def sha256_hex(payload: bytes) -> str:
    """Return lowercase SHA-256 hex digest of in-memory payload."""
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    """Stream file and return lowercase SHA-256 hex digest."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def json_bytes(obj: object) -> bytes:
    """Serialize obj as deterministic sorted-key UTF-8 JSON bytes with trailing newline."""
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")


def run_lock_key(product_id: str, cycle_partition: str, attempt_id: str) -> str:
    """Return canonical ObjectStore key for an attempt's RunLock."""
    return f"runs/{product_id}/{cycle_partition}/attempts/{attempt_id}/run.lock.json"


def solver_receipt_key(product_id: str, cycle_partition: str, attempt_id: str) -> str:
    """Return canonical ObjectStore key for an attempt's verified SolverReceipt."""
    return f"runs/{product_id}/{cycle_partition}/attempts/{attempt_id}/solver_receipt.json"


def post_manifest_key(product_id: str, cycle_partition: str, attempt_id: str) -> str:
    """Return canonical ObjectStore key for an attempt's staged post manifest."""
    return f"runs/{product_id}/{cycle_partition}/attempts/{attempt_id}/post_manifest.json"


def attempt_stage_prefix(product_id: str, cycle_partition: str, attempt_id: str) -> str:
    """Return attempt-scoped staging prefix isolated from live upstream cycles."""
    return f"runs/{product_id}/{cycle_partition}/attempts/{attempt_id}/staged"


def published_cycle_prefix(model_id: str, cycle_partition: str) -> str:
    """Return canonical upstream published cycle prefix."""
    return f"upstream/{model_id}/{cycle_partition}"


def synthetic_cycle_prefix(model_id: str, cycle_partition: str) -> str:
    """Return isolated synthetic fixture prefix outside live upstream/ namespace."""
    return f"fixtures/synthetic/{model_id}/{cycle_partition}"


class ObjectStore(Protocol):
    """Minimal immutable-put, copy, cycle-lock, and opaque-token CAS object storage boundary."""

    def get_bytes(self, key: str) -> bytes | None: ...
    def get_with_token(self, key: str) -> tuple[bytes | None, str | None]: ...
    def put_if_absent(self, key: str, payload: bytes) -> str: ...
    def copy_if_absent(self, src_key: str, dst_key: str, expected_sha256: str) -> str: ...
    def delete_prefix(self, prefix: str) -> None: ...
    def compare_and_swap(self, key: str, payload: bytes, expected_token: str | None) -> str: ...
    def cycle_lock(self, prefix: str) -> AbstractContextManager[None]: ...


class CASConflict(RuntimeError):
    """The current object token differs from the expected token."""


def put_idempotent(
    store: ObjectStore,
    key: str,
    payload: bytes,
    *,
    equiv: Callable[[bytes, bytes], bool] | None = None,
) -> str:
    """Write payload if absent; succeed on retry iff existing matches SHA-256 or equiv()."""
    try:
        return store.put_if_absent(key, payload)
    except FileExistsError:
        existing = store.get_bytes(key)
        if existing is not None and (existing == payload or (equiv and equiv(existing, payload))):
            return sha256_hex(existing)
        raise


def _install_temp_exclusively(tmp_path: Path, dst_path: Path, key: str) -> None:
    """Atomically publish fully written `tmp_path` at `dst_path` without overwriting winners."""
    try:
        os.link(tmp_path, dst_path)
    except FileExistsError as exc:
        raise FileExistsError(f"Immutable object already exists: {key}") from exc


def _cas_token_for_bytes(payload: bytes) -> str:
    return f"etag:{sha256_hex(payload)}"


class LocalFileStore:
    """Filesystem ObjectStore enforcing atomic exclusive puts, hardlinks, cycle lock, and CAS."""

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

    def get_with_token(self, key: str) -> tuple[bytes | None, str | None]:
        raw = self.get_bytes(key)
        if raw is None:
            return None, None
        return raw, _cas_token_for_bytes(raw)

    def put_if_absent(self, key: str, payload: bytes) -> str:
        path = self._resolve(key)
        if path.is_file():
            raise FileExistsError(f"Immutable object already exists: {key}")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        try:
            with tmp.open("xb") as handle:
                handle.write(payload)
                handle.flush()
            _install_temp_exclusively(tmp, path, key)
        finally:
            tmp.unlink(missing_ok=True)
        return sha256_hex(payload)

    def _copy_cross_mount(self, src: Path, dst: Path, src_key: str, dst_key: str, sha: str) -> None:
        tmp = dst.parent / f".{dst.name}.{uuid.uuid4().hex}.tmp"
        try:
            shutil.copy2(src, tmp)
            if sha256_file(tmp) != sha:
                raise ValueError(f"Staged source SHA-256 mismatch on {src_key}")
            try:
                _install_temp_exclusively(tmp, dst, dst_key)
            except FileExistsError:
                if sha256_file(dst) != sha:
                    raise
        finally:
            tmp.unlink(missing_ok=True)

    def copy_if_absent(self, src_key: str, dst_key: str, expected_sha256: str) -> str:
        src = self._resolve(src_key)
        dst = self._resolve(dst_key)
        if dst.is_file():
            if sha256_file(dst) == expected_sha256:
                return expected_sha256
            raise FileExistsError(f"Immutable destination differs from expected SHA-256: {dst_key}")
        if not src.is_file():
            raise FileNotFoundError(f"Missing staged source object: {src_key}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, dst)
        except FileExistsError:
            if sha256_file(dst) != expected_sha256:
                raise
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            self._copy_cross_mount(src, dst, src_key, dst_key, expected_sha256)
        return expected_sha256

    def delete_prefix(self, prefix: str) -> None:
        target = self._resolve(prefix)
        if target.is_dir():
            shutil.rmtree(target)
        elif target.is_file():
            target.unlink()

    @contextlib.contextmanager
    def cycle_lock(self, prefix: str) -> Iterator[None]:
        """Hold exclusive thread + OS flock on `<prefix>/.publish.lock` through promotion."""
        dir_path = self._resolve(prefix)
        dir_path.mkdir(parents=True, exist_ok=True)
        lock_file = dir_path / ".publish.lock"
        thread_lock = _inproc_lock_for(lock_file)
        with thread_lock, lock_file.open("a+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def compare_and_swap(self, key: str, payload: bytes, expected_token: str | None) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.parent / f".{path.name}.cas.lock"
        thread_lock = _inproc_lock_for(lock_path)
        with thread_lock, lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                current = path.read_bytes() if path.is_file() else None
                if current is None:
                    matched = expected_token is None
                    actual_tok: str | None = None
                else:
                    actual_tok = _cas_token_for_bytes(current)
                    matched = expected_token in (actual_tok, actual_tok.removeprefix("etag:"))
                if not matched:
                    raise CASConflict(
                        f"CAS conflict on {key}: expected {expected_token}, found {actual_tok}"
                    )
                tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
                try:
                    with tmp.open("xb") as out_f:
                        out_f.write(payload)
                        out_f.flush()
                    tmp.replace(path)
                finally:
                    tmp.unlink(missing_ok=True)
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        return _cas_token_for_bytes(payload)


def open_store(store_uri: str) -> ObjectStore:
    """Open an explicit local path or file:// URI."""
    if not store_uri.strip():
        raise ValueError("Store path or URI is required")
    parsed = urlparse(store_uri)
    if parsed.scheme in ("", "file"):
        if parsed.scheme == "file" and (parsed.netloc not in ("", "localhost") or not parsed.path):
            raise ValueError("Use a local path or a file:// URI with a local filesystem path")
        raw_path = unquote(parsed.path) if parsed.scheme == "file" else store_uri
        return LocalFileStore(Path(raw_path))
    raise ValueError(f"Unsupported store scheme '{parsed.scheme}://'; use file://")
