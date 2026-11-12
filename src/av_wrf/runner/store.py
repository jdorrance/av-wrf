"""ObjectStore protocol, key builders, streaming hashes, exclusive cycle lock, and CAS."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import shutil
import threading
import unicodedata
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


@contextlib.contextmanager
def _exclusive_lock(path: Path) -> Iterator[None]:
    with _inproc_lock_for(path), path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield


def sha256_hex(payload: bytes) -> str:
    """Return lowercase SHA-256 hex digest of in-memory payload."""
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    """Stream file and return lowercase SHA-256 hex digest."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _copy_sha256_file(source: Path, destination: Path) -> str:
    """Write a new file and hash the bytes copied from its source."""
    digest = hashlib.sha256()
    with source.open("rb") as src, destination.open("xb") as dst:
        while chunk := src.read(1024 * 1024):
            dst.write(chunk)
            digest.update(chunk)
    return digest.hexdigest()


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
    """Exclusive writes return payload SHA-256. CAS uses opaque backend tokens."""

    def get_bytes(self, key: str) -> bytes | None: ...
    def get_with_token(self, key: str) -> tuple[bytes | None, str | None]: ...
    def put_if_absent(self, key: str, payload: bytes) -> str: ...
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


def _ignore_missing_path(_operation: object, _path: str, exc: BaseException) -> None:
    if not isinstance(exc, FileNotFoundError):
        raise exc


def _cas_token_for_bytes(payload: bytes) -> str:
    return f"etag:{sha256_hex(payload)}"


def _sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class LocalFileStore:
    """Atomic, synced writes and exclusive flock on a local POSIX filesystem."""

    def __init__(self, root_dir: Path) -> None:
        self._root = root_dir.resolve()
        self._locks = self._root / ".locks"
        if not self._root.is_dir():
            ancestor = next(parent for parent in self._root.parents if parent.is_dir())
            self._root.mkdir(parents=True, exist_ok=True)
            for parent in self._root.parents:
                _sync_directory(parent)
                if parent == ancestor:
                    break

    def _resolve(self, key: str) -> Path:
        clean = key.lstrip("/")
        if not clean:
            raise ValueError("Store key must be non-empty")
        target = (self._root / clean).resolve()
        if self._root not in target.parents:
            raise ValueError(f"Store key escapes root directory: {key}")
        if target.relative_to(self._root).parts[0].casefold() == self._locks.name:
            raise ValueError("Store key uses the reserved lock directory")
        return target

    def get_bytes(self, key: str) -> bytes | None:
        path = self._resolve(key)
        try:
            return path.read_bytes() if path.is_file() else None
        except FileNotFoundError:
            return None

    def get_with_token(self, key: str) -> tuple[bytes | None, str | None]:
        raw = self.get_bytes(key)
        if raw is None:
            return None, None
        return raw, _cas_token_for_bytes(raw)

    def put_if_absent(self, key: str, payload: bytes) -> str:
        path = self._resolve(key)
        try:
            if path.is_file():
                raise FileExistsError(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_atomic(path, payload, replace=False)
        except FileExistsError as exc:
            # A partial promotion from an earlier writer can be adopted on retry.
            with path.open("rb") as existing:
                os.fsync(existing.fileno())
            self._sync_object_parents(path)
            raise FileExistsError(f"Immutable object already exists: {key}") from exc
        return sha256_hex(payload)

    def _sync_object_parents(self, path: Path) -> None:
        for parent in path.parents:
            _sync_directory(parent)
            if parent == self._root:
                break

    def _write_atomic(self, path: Path, payload: bytes, *, replace: bool) -> None:
        tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
        install = os.replace if replace else os.link
        try:
            with tmp.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            install(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
        self._sync_object_parents(path)

    def delete_prefix(self, prefix: str) -> None:
        target = self._resolve(prefix)
        try:
            if target.is_dir():
                shutil.rmtree(target, onexc=_ignore_missing_path)
            else:
                target.unlink(missing_ok=True)
        except FileNotFoundError:
            pass
        for parent in target.parents:
            try:
                _sync_directory(parent)
                break
            except FileNotFoundError:
                if parent == self._root:
                    raise

    def _lock_path(self, kind: str, target: Path) -> Path:
        self._locks.mkdir(exist_ok=True)
        if self._locks.is_symlink():
            raise ValueError("Store lock directory must be a real directory")
        # Case and normalization aliases share one lock on macOS volumes.
        key = unicodedata.normalize("NFD", target.relative_to(self._root).as_posix()).casefold()
        key_hash = sha256_hex(os.fsencode(key))
        return self._locks / f"{kind}-{key_hash}.lock"

    def cycle_lock(self, prefix: str) -> AbstractContextManager[None]:
        """Hold a stable thread and OS lock through promotion and prefix replacement."""
        return _exclusive_lock(self._lock_path("cycle", self._resolve(prefix)))

    def compare_and_swap(self, key: str, payload: bytes, expected_token: str | None) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _exclusive_lock(self._lock_path("cas", path)):
            _, actual_tok = self.get_with_token(key)
            if expected_token != actual_tok and (
                actual_tok is None or expected_token != actual_tok.removeprefix("etag:")
            ):
                raise CASConflict(
                    f"CAS conflict on {key}: expected {expected_token}, found {actual_tok}"
                )
            self._write_atomic(path, payload, replace=True)
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
