"""Adversarial publication fencing and local crash-fence ordering."""

from __future__ import annotations

import contextlib
import os
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from av_wrf.contracts import ReadyReceipt
from av_wrf.runner import (
    LocalFileStore,
    ObjectStore,
    load_bundle,
    load_ready_receipt,
    post_manifest_key,
    publish_cycle_receipt,
    reconcile_dispatch,
)
from av_wrf.runner.publish import _advance_latest_pointer_with_retry
from av_wrf.runner.store import _sync_directory
from tests.integration import stage_sparse_config
from tests.integration.test_local_publication_contract import receipt as receipt
from tests.integration.test_publish_recovery import ROOT, _stage_attempt_with_t2


def test_remote_paused_writer_cannot_block_another_attempt(tmp_path: Path) -> None:
    started, resume = threading.Event(), threading.Event()
    cfg = tmp_path / "cfg"
    product = stage_sparse_config(ROOT / "config", cfg)
    bundle = load_bundle(cfg, product)
    stage, backend = LocalFileStore(tmp_path / "stage"), LocalFileStore(tmp_path / "dest")
    destination = Mock(spec=ObjectStore, wraps=backend)
    destination.cycle_lock.side_effect = lambda _: contextlib.nullcontext()
    origin = f"2026-11-12T{bundle.product.publish_origin_hour_utc:02d}:00:00Z"
    part = _stage_attempt_with_t2(cfg, stage, bundle, origin, "attempt-a", 285.0)
    _stage_attempt_with_t2(cfg, stage, bundle, origin, "attempt-b", 305.0)

    def put(key: str, payload: bytes) -> str:
        result = backend.put_if_absent(key, payload)
        if "/attempts/attempt-a/" in key and key.endswith("/forecast.zarr/zarr.json"):
            started.set()
            assert resume.wait(10)
        return result

    destination.put_if_absent.side_effect = put

    def publish(attempt: str) -> str:
        return publish_cycle_receipt(
            cfg,
            destination,
            product_id=product,
            cycle_partition=part,
            attempt_id=attempt,
            bundle=bundle,
            stage_store=stage,
        ).attempt_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        paused = pool.submit(publish, "attempt-a")
        try:
            assert started.wait(10)
            assert publish("attempt-b") == "attempt-b"
        finally:
            resume.set()
        with pytest.raises(FileExistsError):
            paused.result()
    receipt = load_ready_receipt(destination, bundle.product.model_id, part)
    assert receipt.attempt_id == "attempt-b"
    assert receipt.zarr_relpath.startswith("attempts/attempt-b/")
    assert publish("attempt-b") == "attempt-b"


def test_local_cycle_syncs_unique_directories_before_rename(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    synced: list[Path] = []

    def sync(path: Path) -> None:
        synced.append(path)
        _sync_directory(path)

    prefix = "upstream/model/20261112/00"
    with (
        patch("av_wrf.runner.store._sync_directory", side_effect=sync),
        store.cycle_lock(prefix),
        store._cycle_writer(prefix) as write,
    ):
        write("forecast.zarr/zarr.json", b"metadata")
        write("forecast.zarr/other", b"data")
        write("_ready.json", b"ready")
        assert store.get_bytes(f"{prefix}/_ready.json") is None
    scratch = (tmp_path / prefix).with_name(".00.promotion")
    assert synced[:2] == [scratch / "forecast.zarr", scratch]
    assert len(synced) == len(set(synced))
    assert store.get_bytes(f"{prefix}/_ready.json") == b"ready"


def test_local_writes_sync_all_parents_bottom_up(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    synced: list[Path] = []

    def sync(path: Path) -> None:
        synced.append(path)
        _sync_directory(path)

    with patch("av_wrf.runner.store._sync_directory", side_effect=sync):
        store.put_if_absent("a/b/c/object", b"data")
    assert synced == [tmp_path / "a/b/c", tmp_path / "a/b", tmp_path / "a", tmp_path]


def test_local_cycle_does_not_commit_after_directory_walk_error(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    scandir = os.scandir

    def fail_nested(path: str | Path | int) -> Iterator[os.DirEntry[str]]:
        if isinstance(path, str) and path.endswith("/forecast.zarr"):
            raise OSError("cannot read shard directory")
        return scandir(path)

    prefix = "upstream/model/20261112/00"
    with (
        patch("av_wrf.runner.store.os.scandir", side_effect=fail_nested),
        store.cycle_lock(prefix),
        pytest.raises(OSError, match="cannot read shard directory"),
        store._cycle_writer(prefix) as write,
    ):
        write("forecast.zarr/zarr.json", b"metadata")
        write("_ready.json", b"ready")
    assert store.get_bytes(f"{prefix}/_ready.json") is None


def test_slow_dispatch_claim_expires_before_send(tmp_path: Path, receipt: ReadyReceipt) -> None:
    store = LocalFileStore(tmp_path)
    send = Mock(return_value=204)
    with (
        patch("av_wrf.runner.dispatch.monotonic", side_effect=(0.0, 6.0)),
        pytest.raises(RuntimeError, match="Dispatch lease expired"),
    ):
        reconcile_dispatch(store, receipt, send, lease_seconds=5, now_unix_s=0)
    send.assert_not_called()
    assert reconcile_dispatch(store, receipt, send, lease_seconds=5, now_unix_s=6)
    send.assert_called_once_with(receipt)


def test_data_sync_failure_never_installs_post_manifest(tmp_path: Path) -> None:
    class FailingDataSync(LocalFileStore):
        def _sync_object_parents(self, path: Path) -> None:
            if "forecast.zarr" in path.parts:
                raise OSError("data sync failed")
            super()._sync_object_parents(path)

    cfg = tmp_path / "cfg"
    product = stage_sparse_config(ROOT / "config", cfg)
    bundle = load_bundle(cfg, product)
    store = FailingDataSync(tmp_path / "store")
    origin = f"2026-11-12T{bundle.product.publish_origin_hour_utc:02d}:00:00Z"
    with pytest.raises(OSError, match="data sync failed"):
        _stage_attempt_with_t2(cfg, store, bundle, origin, "attempt-a", 285.0)
    partition = f"20261112/{bundle.product.publish_origin_hour_utc:02d}"
    assert store.get_bytes(post_manifest_key(product, partition, "attempt-a")) is None


@pytest.mark.parametrize("pruned", [False, True])
def test_ready_recovery_adopts_after_directory_sync_failure(tmp_path: Path, pruned: bool) -> None:
    cfg = tmp_path / "cfg"
    product = stage_sparse_config(ROOT / "config", cfg)
    bundle = load_bundle(cfg, product)
    stage, dst = LocalFileStore(tmp_path / "stage"), LocalFileStore(tmp_path / "dst")
    origin = f"2026-11-12T{bundle.product.publish_origin_hour_utc:02d}:00:00Z"
    part = _stage_attempt_with_t2(cfg, stage, bundle, origin, "attempt-a", 285.0)
    ready = f"upstream/{bundle.product.model_id}/{part}/_ready.json"
    latest = f"upstream/{bundle.product.model_id}/latest.json"
    sync = dst._sync_object_parents

    def fail_ready(path: Path) -> None:
        if path.name == "_ready.json":
            raise OSError("ready directory sync failed")
        sync(path)

    def publish() -> None:
        publish_cycle_receipt(
            cfg,
            dst,
            product_id=product,
            cycle_partition=part,
            attempt_id="attempt-a",
            bundle=bundle,
            stage_store=stage,
            now_utc_iso=origin,
        )

    with (
        patch.object(dst, "_sync_object_parents", side_effect=fail_ready),
        pytest.raises(OSError, match="ready directory sync failed"),
    ):
        publish()
    assert dst.get_bytes(ready) is not None and dst.get_bytes(latest) is None
    if pruned:
        stage.delete_prefix(post_manifest_key(product, part, "attempt-a"))
    with (
        patch.object(dst, "_sync_object_parents", side_effect=fail_ready),
        pytest.raises(OSError, match="ready directory sync failed"),
    ):
        publish()
    assert dst.get_bytes(latest) is None
    publish()
    assert dst.get_bytes(latest) is not None


@pytest.mark.parametrize("newer", [False, True])
def test_latest_recovery_adopts_failed_cas(tmp_path: Path, newer: bool) -> None:
    cfg = tmp_path / "cfg"
    product = stage_sparse_config(ROOT / "config", cfg)
    bundle = load_bundle(cfg, product)
    stage, dst = LocalFileStore(tmp_path / "stage"), LocalFileStore(tmp_path / "dst")
    origin = f"2026-11-12T{bundle.product.publish_origin_hour_utc:02d}:00:00Z"
    part = _stage_attempt_with_t2(cfg, stage, bundle, origin, "attempt-a", 285.0)
    receipt = publish_cycle_receipt(
        cfg,
        dst,
        product_id=product,
        cycle_partition=part,
        attempt_id="attempt-a",
        bundle=bundle,
        stage_store=stage,
        now_utc_iso="2020-01-01T00:00:00Z",
    )
    latest = f"upstream/{receipt.model_id}/latest.json"
    sync = dst._sync_object_parents

    def fail_latest(path: Path) -> None:
        if path.name == "latest.json":
            raise OSError("latest directory sync failed")
        sync(path)

    with patch.object(dst, "_sync_object_parents", side_effect=fail_latest):
        with pytest.raises(OSError, match="latest directory sync failed"):
            _advance_latest_pointer_with_retry(dst, receipt, bundle, origin)
        prior = replace(receipt, publish_origin_utc=origin.replace(":00:00Z", ":00:00.001Z"))
        target = receipt if not newer else prior
        if newer:
            dst.delete_prefix(latest)
            with pytest.raises(OSError, match="latest directory sync failed"):
                _advance_latest_pointer_with_retry(dst, prior, bundle, origin)
            target = receipt
        with pytest.raises(OSError, match="latest directory sync failed"):
            _advance_latest_pointer_with_retry(dst, target, bundle, origin)
    assert _advance_latest_pointer_with_retry(dst, target, bundle, origin) == (
        "newer" if newer else "current"
    )


def test_durability_barrier_serializes_with_cas_replacement(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    key = "upstream/model/latest.json"
    token = store.compare_and_swap(key, b"old", None)
    installed, release, started, synced = (threading.Event() for _ in range(4))
    sync = store._sync_object_parents

    def pause_writer(path: Path) -> None:
        if threading.current_thread().name.startswith("writer"):
            installed.set()
            assert release.wait(5)
            raise OSError("directory sync failed")
        sync(path)
        synced.set()

    def adopt() -> None:
        started.set()
        store.ensure_durable(key)

    with (
        patch.object(store, "_sync_object_parents", side_effect=pause_writer),
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="writer") as writers,
        ThreadPoolExecutor(max_workers=1) as adopters,
    ):
        writer = writers.submit(store.compare_and_swap, key, b"new", token)
        try:
            assert installed.wait(5)
            adoption = adopters.submit(adopt)
            assert started.wait(5)
            assert not synced.wait(0.1)
        finally:
            release.set()
        with pytest.raises(OSError, match="directory sync failed"):
            writer.result()
        adoption.result()
    assert synced.is_set() and store.get_bytes(key) == b"new"
