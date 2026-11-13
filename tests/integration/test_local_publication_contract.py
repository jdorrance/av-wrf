"""Exercise local publication locking, latest ordering, and webhook transport."""

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import replace
from functools import partial
from pathlib import Path
from threading import Event
from traceback import format_exception
from unittest.mock import patch

import httpx
import pytest

from av_wrf.contracts import BuildManifest, ReadyReceipt, RunLock, SolverReceipt
from av_wrf.runner import (
    LoadedBundle,
    LocalFileStore,
    ObjectStore,
    attempt_stage_prefix,
    dispatch_idempotency_key,
    json_bytes,
    load_bundle,
    load_ready_receipt,
    plan_product_cycle,
    post_cycle_from_inputs,
    post_webhook_json,
    publish_cycle_receipt,
    published_cycle_prefix,
    put_idempotent,
    reconcile_dispatch,
    solver_receipt_key,
)
from av_wrf.runner.forcing_gfs import GFS
from av_wrf.runner.publish import _advance_latest_pointer_with_retry
from av_wrf.runner.store import CASConflict, synthetic_cycle_prefix
from tests.integration import bundle_for_recipe, ready_with_diagnostics, stage_sparse_config


@pytest.fixture
def receipt(tmp_path: Path) -> ReadyReceipt:
    result = ReadyReceipt(
        "wrf-zarr-v3",
        1,
        "model",
        "20261112/00",
        "2026-11-12T00:00:00Z",
        "att-01",
        "forecast.zarr",
        {},
        38,
        (),
        (),
        (),
        (),
        (),
        (),
        "config",
        "source",
        "image",
    )
    return ready_with_diagnostics(LocalFileStore(tmp_path), result)


def test_cycle_lock_survives_prefix_removal_and_delete_syncs_parent(tmp_path: Path) -> None:
    store = LocalFileStore(tmp_path)
    prefix = "upstream/model/20261112/00"
    key = f"{prefix}/_promotion_owner.json"
    store.compare_and_swap(key, b"owner", None)
    locks = tmp_path / ".locks"
    with store.cycle_lock(prefix):
        lock_inodes = {lock.name: lock.stat().st_ino for lock in locks.iterdir()}
        assert len(lock_inodes) == 2
        with patch("av_wrf.runner.store._sync_directory") as sync:
            store.delete_prefix(prefix)
            sync.assert_called_once_with((tmp_path / prefix).parent)
        assert not (tmp_path / prefix).exists()
        assert {lock.name: lock.stat().st_ino for lock in locks.iterdir()} == lock_inodes
        for lock_dir in (".locks", ".LOCKS"):
            with pytest.raises(ValueError, match="reserved lock directory"):
                store.delete_prefix(lock_dir)
    with store.cycle_lock(prefix.upper()):
        assert {lock.name: lock.stat().st_ino for lock in locks.iterdir()} == lock_inodes


@pytest.fixture
def planned_attempt(tmp_path: Path) -> tuple[Path, LocalFileStore, LoadedBundle, RunLock]:
    config = tmp_path / "config"
    product = stage_sparse_config(Path(__file__).resolve().parents[2] / "config", config)
    bundle = load_bundle(config, product)
    store = LocalFileStore(tmp_path / "store")
    _, lock = plan_product_cycle(
        config,
        store,
        product_id=product,
        publish_origin_utc=f"2026-11-12T{bundle.product.publish_origin_hour_utc:02d}:00:00Z",
        attempt_id="att-local-publish",
        mpi_ranks=1,
        build_manifest=BuildManifest("sha256:" + "b" * 64, "w", "p", (), "gcc", "mpi"),
        geog_bundle_sha256="a" * 64,
        bundle=bundle,
    )
    return config, store, bundle, lock


def test_pruning_waits_for_repost(
    planned_attempt: tuple[Path, LocalFileStore, LoadedBundle, RunLock], tmp_path: Path
) -> None:
    config, store, bundle, lock = planned_attempt
    product = lock.product_id
    stage_root = attempt_stage_prefix(product, lock.cycle_partition, lock.attempt_id)

    def post() -> None:
        post_cycle_from_inputs(
            config,
            store,
            product_id=product,
            cycle_partition=lock.cycle_partition,
            attempt_id=lock.attempt_id,
            wrfout_dir=None,
            synthetic=True,
            bundle=bundle,
        )

    post()
    repost_paused, resume_repost, publish_at_stage_lock = Event(), Event(), Event()
    cycle_lock = store.cycle_lock

    def mark_stage_lock(prefix: str) -> AbstractContextManager[None]:
        if prefix == stage_root and repost_paused.is_set():
            publish_at_stage_lock.set()
        return cycle_lock(prefix)

    def pause_first_write(
        store: ObjectStore,
        key: str,
        payload: bytes,
        *,
        equiv: Callable[[bytes, bytes], bool] | None = None,
    ) -> str:
        digest = put_idempotent(store, key, payload, equiv=equiv)
        if not repost_paused.is_set():
            repost_paused.set()
            assert resume_repost.wait(timeout=5)
        return digest

    with (
        patch.object(store, "cycle_lock", side_effect=mark_stage_lock),
        patch("av_wrf.runner.post.put_idempotent", side_effect=pause_first_write),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        repost = pool.submit(post)
        try:
            assert repost_paused.wait(timeout=5)
            promotion = pool.submit(
                publish_cycle_receipt,
                config,
                store,
                product_id=product,
                cycle_partition=lock.cycle_partition,
                attempt_id=lock.attempt_id,
                bundle=bundle,
                allow_synthetic=True,
                prune_stage=True,
            )
            assert publish_at_stage_lock.wait(timeout=5)
        finally:
            resume_repost.set()
        repost.result()
        assert promotion.result().attempt_id == lock.attempt_id
    assert not (tmp_path / "store" / stage_root).exists()


def test_pruned_retry_finds_matching_receipt_in_allowed_namespace(
    planned_attempt: tuple[Path, LocalFileStore, LoadedBundle, RunLock],
) -> None:
    config, store, bundle, lock = planned_attempt
    post_cycle_from_inputs(
        config,
        store,
        product_id=lock.product_id,
        cycle_partition=lock.cycle_partition,
        attempt_id=lock.attempt_id,
        bundle=bundle,
        wrfout_dir=None,
        synthetic=True,
    )
    retry = partial(
        publish_cycle_receipt,
        config,
        store,
        product_id=lock.product_id,
        cycle_partition=lock.cycle_partition,
        attempt_id=lock.attempt_id,
        bundle=bundle,
        now_utc_iso=lock.publish_origin_utc,
        prune_stage=True,
    )
    fixture = retry(allow_synthetic=True)
    live = replace(
        fixture,
        forcing_profile_id=lock.forcing_profile_id,
        oci_digest=lock.build_manifest.oci_digest,
    )
    live_root = published_cycle_prefix(lock.model_id, lock.cycle_partition)
    fixture_root = synthetic_cycle_prefix(lock.model_id, lock.cycle_partition)
    live_key, fixture_key = f"{live_root}/_ready.json", f"{fixture_root}/_ready.json"
    solver = SolverReceipt(
        product_id=lock.product_id,
        cycle_partition=lock.cycle_partition,
        attempt_id=lock.attempt_id,
        config_sha256=lock.config_sha256,
        oci_digest=live.oci_digest,
        geog_sha256=lock.geog_bundle_sha256,
        forcing_sha256s=(("forcing", "d" * 64),),
        wrfout_sha256s=(("wrfout", "e" * 64),),
    )
    store.put_if_absent(
        solver_receipt_key(lock.product_id, lock.cycle_partition, lock.attempt_id),
        json_bytes(solver.to_doc()),
    )
    for path in ("_diagnostics.json", *(f"forecast.zarr/{key}" for key, _ in live.shard_sha256s)):
        payload = store.get_bytes(f"{fixture_root}/{path}")
        assert payload is not None
        store.put_if_absent(f"{live_root}/{path}", payload)

    store.put_if_absent(live_key, json_bytes(live.to_doc()))
    store.delete_prefix(fixture_key)
    store.put_if_absent(fixture_key, json_bytes(replace(fixture, attempt_id="competing").to_doc()))
    assert retry(allow_synthetic=True) == live
    store.delete_prefix(live_key)
    with pytest.raises(FileExistsError, match="competing"):
        retry(allow_synthetic=True)
    store.delete_prefix(fixture_key)
    store.put_if_absent(fixture_key, json_bytes(fixture.to_doc()))
    with pytest.raises(FileNotFoundError, match=r"Missing post_manifest\.json"):
        retry(allow_synthetic=False)
    store.put_if_absent(live_key, json_bytes(replace(live, attempt_id="competing").to_doc()))
    assert retry(allow_synthetic=True) == fixture
    with pytest.raises(FileExistsError, match="competing"):
        retry(allow_synthetic=False)

    fixture_path = config.parent / "store" / fixture_key
    fixture_path.write_bytes(
        json_bytes(replace(fixture, shard_sha256s=fixture.shard_sha256s[:-1]).to_doc())
    )
    with pytest.raises(ValueError, match="Published Zarr inventory"):
        retry(allow_synthetic=True)
    fixture_path.write_bytes(json_bytes(fixture.to_doc()))

    shard_key = f"{fixture_root}/forecast.zarr/{fixture.shard_sha256s[-1][0]}"
    store.delete_prefix(shard_key)
    with pytest.raises(ValueError, match="Corrupt or missing Zarr object"):
        retry(allow_synthetic=True)
    store.put_if_absent(shard_key, b"corrupt shard")
    with pytest.raises(ValueError, match="Corrupt or missing Zarr object"):
        retry(allow_synthetic=True)

    store.delete_prefix(live_key)
    store.put_if_absent(live_key, json_bytes(fixture.to_doc()))
    with pytest.raises(ValueError, match="execution mode"):
        retry(allow_synthetic=True)
    with pytest.raises(ValueError, match="execution mode"):
        load_ready_receipt(store, lock.model_id, lock.cycle_partition)
    for wrong in (
        replace(live, forcing_profile_id="wrong-source"),
        replace(live, oci_digest="sha256:" + "0" * 64),
        replace(live, publish_origin_utc="2026-11-11T00:00:00Z"),
    ):
        store.delete_prefix(live_key)
        store.put_if_absent(live_key, json_bytes(wrong.to_doc()))
        with pytest.raises(ValueError, match="Published receipt provenance"):
            retry(allow_synthetic=True)


def test_latest_pointer_compares_instants(tmp_path: Path, receipt: ReadyReceipt) -> None:
    b = bundle_for_recipe(Path(__file__).resolve().parents[2] / "config", GFS)
    hour = b.product.publish_origin_hour_utc
    receipt = replace(receipt, publish_origin_utc=f"2026-11-12T{hour:02}:00:00Z")
    store = LocalFileStore(tmp_path)
    latest_key = "upstream/model/latest.json"
    doc = {
        "model_id": receipt.model_id,
        "cycle_partition": f"20261112/{hour:02}",
        "publish_origin_utc": f"2026-11-12T{hour:02}:00:00.001+00:00",
        "ready_key": f"upstream/{receipt.model_id}/20261112/{hour:02}/_ready.json",
    }
    newer = json_bytes(doc)
    token = store.compare_and_swap(latest_key, newer, None)
    now = f"2026-11-12T{hour:02}:15:00Z"
    assert _advance_latest_pointer_with_retry(store, receipt, b, now) == "newer"
    assert store.get_bytes(latest_key) == newer
    token = store.compare_and_swap(latest_key, json_bytes({**doc, "ready_key": "wrong"}), token)
    with pytest.raises(ValueError, match=r"Invalid cycle identity in latest\.json"):
        _advance_latest_pointer_with_retry(store, receipt, b, now)
    store.compare_and_swap(latest_key, b"{}", token)
    with pytest.raises(ValueError, match=r"Invalid publish origin in latest\.json"):
        _advance_latest_pointer_with_retry(store, receipt, b, now)
    late = f"2026-11-13T{hour:02d}:15:00Z"
    assert _advance_latest_pointer_with_retry(store, receipt, b, late) == "outside_live_window"
    store.delete_prefix(latest_key)
    assert _advance_latest_pointer_with_retry(store, receipt, b, now) == "advanced"
    assert _advance_latest_pointer_with_retry(store, receipt, b, now) == "current"


def test_rejected_webhook_can_retry_delivery(tmp_path: Path, receipt: ReadyReceipt) -> None:
    store = LocalFileStore(tmp_path)
    with pytest.raises(RuntimeError, match=r"Consumer webhook rejected .*: 503"):
        reconcile_dispatch(store, receipt, lambda _: 503)
    assert reconcile_dispatch(store, receipt, lambda _: 204)
    assert not reconcile_dispatch(store, receipt, lambda _: pytest.fail("Already delivered"))


def test_dispatch_adopts_lost_write_responses(tmp_path: Path, receipt: ReadyReceipt) -> None:
    store = LocalFileStore(tmp_path)
    cas = store.compare_and_swap

    def lose_response(key: str, payload: bytes, expected_token: str | None) -> str:
        cas(key, payload, expected_token)
        raise CASConflict("The retry hit the newly committed value")

    with patch.object(store, "compare_and_swap", side_effect=lose_response):
        assert reconcile_dispatch(store, receipt, lambda _: 204)
        assert not reconcile_dispatch(store, receipt, lambda _: pytest.fail("Already delivered"))


def test_webhook_tls_bearer_and_error_redaction(receipt: ReadyReceipt) -> None:
    url = "https://example.invalid/endpoint"
    token = "secret-token"
    with patch("av_wrf.runner.dispatch.httpx.post", return_value=httpx.Response(202)) as post:
        for unsafe in (
            "http://example.invalid",
            f"https://{token}@example.invalid",
            f"{url}?x={token}",
        ):
            with pytest.raises(ValueError, match="requires HTTPS"):
                post_webhook_json(unsafe, receipt, bearer_token=token)
        post.assert_not_called()
        assert post_webhook_json(url, receipt, bearer_token=token) == 202
        assert post.call_args.kwargs["headers"]["Authorization"] == f"Bearer {token}"
        assert post.call_args.kwargs["headers"]["Idempotency-Key"] == dispatch_idempotency_key(
            receipt, url
        )
        assert post.call_args.kwargs["follow_redirects"] is False
        post.side_effect = httpx.ConnectError(f"Sensitive destination {url}/{token}")
        with pytest.raises(RuntimeError, match="Consumer webhook request failed") as failure:
            post_webhook_json(url, receipt, bearer_token=token)
        assert token not in "".join(format_exception(failure.value))
