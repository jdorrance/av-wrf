"""ObjectStore protocol, key builders, atomic link-install writes, and flock CAS."""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse


def sha256_hex(payload: bytes) -> str:
    """Return lowercase SHA-256 hex digest of payload."""
    return hashlib.sha256(payload).hexdigest()


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
    """Minimal immutable-put, store-native copy, cleanup, and CAS object storage boundary."""

    def get_bytes(self, key: str) -> bytes | None: ...
    def put_if_absent(self, key: str, payload: bytes) -> str: ...
    def copy_if_absent(self, src_key: str, dst_key: str, expected_sha256: str) -> str: ...
    def delete_prefix(self, prefix: str) -> None: ...
    def compare_and_swap(self, key: str, payload: bytes, expected_sha256: str | None) -> str: ...


def put_idempotent(
    store: ObjectStore,
    key: str,
    payload: bytes,
    *,
    equiv: Callable[[bytes, bytes], bool] | None = None,
) -> str:
    """Write payload if absent; succeed on retry iff existing matches SHA-256 or equiv()."""
    digest = sha256_hex(payload)
    try:
        return store.put_if_absent(key, payload)
    except FileExistsError:
        existing = store.get_bytes(key)
        if existing is not None:
            if sha256_hex(existing) == digest:
                return digest
            if equiv is not None and equiv(existing, payload):
                return sha256_hex(existing)
        raise


def _install_temp_exclusively(tmp_path: Path, dst_path: Path, key: str) -> None:
    """Atomically publish fully written `tmp_path` at `dst_path` without overwriting winners."""
    try:
        os.link(tmp_path, dst_path)
    except FileExistsError as exc:
        raise FileExistsError(f"Immutable object already exists: {key}") from exc


class LocalFileStore:
    """Filesystem ObjectStore enforcing atomic exclusive puts, hardlinks, and flock CAS."""

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
            if sha256_hex(tmp.read_bytes()) != sha:
                raise ValueError(f"Staged source SHA-256 mismatch on {src_key}")
            try:
                _install_temp_exclusively(tmp, dst, dst_key)
            except FileExistsError:
                if sha256_hex(dst.read_bytes()) != sha:
                    raise
        finally:
            tmp.unlink(missing_ok=True)

    def copy_if_absent(self, src_key: str, dst_key: str, expected_sha256: str) -> str:
        src = self._resolve(src_key)
        dst = self._resolve(dst_key)
        if dst.is_file():
            if sha256_hex(dst.read_bytes()) == expected_sha256:
                return expected_sha256
            raise FileExistsError(f"Immutable destination differs from expected SHA-256: {dst_key}")
        if not src.is_file():
            raise FileNotFoundError(f"Missing staged source object: {src_key}")
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(src, dst)
        except FileExistsError:
            if sha256_hex(dst.read_bytes()) != expected_sha256:
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

    def compare_and_swap(self, key: str, payload: bytes, expected_sha256: str | None) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.parent / f".{path.name}.cas.lock"
        with lock_path.open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                current = path.read_bytes() if path.is_file() else None
                actual = sha256_hex(current) if current is not None else None
                if actual != expected_sha256:
                    raise RuntimeError(
                        f"CAS conflict on {key}: expected {expected_sha256}, found {actual}"
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
        return sha256_hex(payload)


def open_store(store_uri: str) -> ObjectStore:
    """Open an ObjectStore from a file:// URI (S3/R2 obstore wiring lands in Phase 2)."""
    parsed = urlparse(store_uri)
    if parsed.scheme in ("", "file"):
        raw_path = parsed.path if parsed.scheme == "file" else store_uri
        return LocalFileStore(Path(raw_path))
    raise ValueError(
        f"Unsupported scheme '{parsed.scheme}://' in Phase 1; use file:// until Phase 2"
    )
