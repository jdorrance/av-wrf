"""Strict full-horizon `publish` promotion, synthetic fixture fence, and recoverable CAS."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from av_wrf.contracts import (
    WRF_ZARR_V3_FIELDS,
    ZARR_SCHEMA_ID,
    ZARR_SCHEMA_VERSION,
    CycleWindow,
    ReadyReceipt,
    RunLock,
    StagedPostManifest,
    format_iso_utc,
)
from av_wrf.core import (
    is_cycle_live_fresh,
    published_lambert_grid_metadata,
    resolve_cycle_window,
)
from av_wrf.runner.bundle import LoadedBundle, load_bundle
from av_wrf.runner.publish_verify import validate_post_manifest, verify_staged_objects
from av_wrf.runner.solver import verify_solver_receipt
from av_wrf.runner.stages import load_run_lock
from av_wrf.runner.store import (
    ObjectStore,
    attempt_stage_prefix,
    json_bytes,
    post_manifest_key,
    published_cycle_prefix,
    put_idempotent,
    sha256_hex,
    synthetic_cycle_prefix,
)

MAX_CAS_RETRIES = 8


def _acquire_promotion_owner(
    store: ObjectStore, pub_root: str, attempt_id: str
) -> tuple[str, bool]:
    owner_key = f"{pub_root}/_promotion_owner.json"
    next_bytes = json_bytes({"attempt_id": attempt_id})
    for _ in range(MAX_CAS_RETRIES):
        cur = store.get_bytes(owner_key)
        prev_sha = sha256_hex(cur) if cur is not None else None
        prev_attempt = json.loads(cur.decode("utf-8")).get("attempt_id") if cur else None
        try:
            sha = store.compare_and_swap(owner_key, next_bytes, prev_sha)
            return sha, (prev_attempt is not None and prev_attempt != attempt_id)
        except RuntimeError:
            continue
    raise RuntimeError(f"Failed to acquire promotion ownership on {pub_root}")


def _copy_one_staged_item(
    stage_store: ObjectStore, dst_store: ObjectStore, src_k: str, dst_k: str, sha: str
) -> None:
    if stage_store is dst_store:
        dst_store.copy_if_absent(src_k, dst_k, sha)
        return
    raw = stage_store.get_bytes(src_k)
    if raw is None or sha256_hex(raw) != sha:
        raise ValueError(f"Missing staged object during cross-store copy: {src_k}")
    put_idempotent(dst_store, dst_k, raw)


def _promote_verified_objects(
    stage_store: ObjectStore,
    dst_store: ObjectStore,
    stage_root: str,
    pub_root: str,
    pm: StagedPostManifest,
    attempt_id: str,
) -> None:
    _, had_prior_owner = _acquire_promotion_owner(dst_store, pub_root, attempt_id)
    if dst_store.get_bytes(f"{pub_root}/_ready.json") is not None:
        return
    staged_zarr, pub_zarr = f"{stage_root}/{pm.zarr_relpath}", f"{pub_root}/{pm.zarr_relpath}"
    if had_prior_owner:
        dst_store.delete_prefix(pub_zarr)
        dst_store.delete_prefix(f"{pub_root}/_diagnostics.json")
    all_items = [
        *(
            (f"{staged_zarr}/{k}", f"{pub_zarr}/{k}", s)
            for k, s in (*pm.meta_sha256s, *pm.shard_sha256s)
        ),
        (f"{stage_root}/_diagnostics.json", f"{pub_root}/_diagnostics.json", pm.diagnostics_sha256),
    ]
    for idx, (src_k, dst_k, sha) in enumerate(all_items):
        try:
            _copy_one_staged_item(stage_store, dst_store, src_k, dst_k, sha)
        except FileExistsError:
            if dst_store.get_bytes(f"{pub_root}/_ready.json") is not None:
                raise
            dst_store.delete_prefix(pub_zarr)
            dst_store.delete_prefix(f"{pub_root}/_diagnostics.json")
            for r_src, r_dst, r_sha in all_items[: idx + 1]:
                _copy_one_staged_item(stage_store, dst_store, r_src, r_dst, r_sha)


def _advance_latest_pointer_with_retry(
    store: ObjectStore, receipt: ReadyReceipt, b: LoadedBundle, now_utc_iso: str
) -> None:
    if not is_cycle_live_fresh(b.product, b.output, receipt.publish_origin_utc, now_utc_iso):
        return
    latest_key = f"upstream/{receipt.model_id}/latest.json"
    ready_key = f"{published_cycle_prefix(receipt.model_id, receipt.cycle_partition)}/_ready.json"
    next_payload = json_bytes(
        {
            "model_id": receipt.model_id,
            "cycle_partition": receipt.cycle_partition,
            "publish_origin_utc": receipt.publish_origin_utc,
            "ready_key": ready_key,
        }
    )
    for _ in range(MAX_CAS_RETRIES):
        current_raw = store.get_bytes(latest_key)
        expected_sha = sha256_hex(current_raw) if current_raw is not None else None
        if current_raw is not None:
            cur_doc = json.loads(current_raw.decode("utf-8"))
            if (
                receipt.publish_origin_utc < str(cur_doc["publish_origin_utc"])
                or cur_doc.get("ready_key") == ready_key
            ):
                return
        try:
            store.compare_and_swap(latest_key, next_payload, expected_sha)
            return
        except RuntimeError:
            continue
    raise RuntimeError(f"Failed to advance {latest_key} after {MAX_CAS_RETRIES} CAS attempts")


def _check_existing_published_ready(
    stage_store: ObjectStore,
    dst_store: ObjectStore,
    ready_key: str,
    stage_root: str,
    lock: RunLock,
    b: LoadedBundle,
    is_synthetic: bool,
    now_iso: str,
) -> ReadyReceipt | None:
    existing_ready = dst_store.get_bytes(ready_key)
    if existing_ready is None:
        return None
    existing_doc = json.loads(existing_ready.decode("utf-8"))
    if not isinstance(existing_doc, dict):
        raise ValueError("Corrupt _ready.json in published store")
    committed = ReadyReceipt.from_doc(existing_doc)
    if committed.attempt_id != lock.attempt_id or committed.config_sha256 != lock.config_sha256:
        raise FileExistsError(
            f"Cycle {lock.cycle_partition} already published by {committed.attempt_id}"
        )
    if not is_synthetic:
        _advance_latest_pointer_with_retry(dst_store, committed, b, now_iso)
    stage_store.delete_prefix(stage_root)
    return committed


def _build_ready_receipt(
    lock: RunLock, pm: StagedPostManifest, b: LoadedBundle, window: CycleWindow, is_synth: bool
) -> ReadyReceipt:
    grid_meta = {
        **published_lambert_grid_metadata(b.domain),
        "demSource": b.domain.dem_source,
        "landuseSource": b.domain.landuse_source,
    }
    return ReadyReceipt(
        ZARR_SCHEMA_ID,
        ZARR_SCHEMA_VERSION,
        lock.model_id,
        lock.cycle_partition,
        lock.publish_origin_utc,
        lock.attempt_id,
        pm.zarr_relpath,
        grid_meta,
        b.domain.exported_levels_k,
        pm.lead_hours,
        pm.valid_times_utc,
        window.window_starts_utc,
        pm.valid_times_utc,
        tuple(f.name for f in WRF_ZARR_V3_FIELDS),
        (*pm.meta_sha256s, *pm.shard_sha256s),
        lock.config_sha256,
        "synthetic-fixture" if is_synth else lock.forcing_profile_id,
        "synthetic-fixture" if is_synth else lock.build_manifest.oci_digest,
    )


def publish_cycle_receipt(
    config_root: Path,
    store: ObjectStore,
    *,
    product_id: str,
    cycle_partition: str,
    attempt_id: str,
    bundle: LoadedBundle | None = None,
    stage_store: ObjectStore | None = None,
    allow_synthetic: bool = False,
    now_utc_iso: str | None = None,
) -> ReadyReceipt:
    """Verify RunLock provenance, ZEP-0002 shards & coords, recoverably promote, and CAS latest."""
    stg = store if stage_store is None else stage_store
    now_iso = format_iso_utc(datetime.now(UTC)) if now_utc_iso is None else now_utc_iso
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    lock = load_run_lock(stg, product_id, cycle_partition, attempt_id)
    if lock.config_sha256 != b.config_sha256:
        raise ValueError("Config bundle SHA-256 drifted from frozen RunLock")
    stage_root = attempt_stage_prefix(product_id, cycle_partition, attempt_id)
    pm_raw = stg.get_bytes(post_manifest_key(product_id, cycle_partition, attempt_id))
    if pm_raw is None:
        live_r = f"{published_cycle_prefix(lock.model_id, lock.cycle_partition)}/_ready.json"
        fix_r = f"{synthetic_cycle_prefix(lock.model_id, lock.cycle_partition)}/_ready.json"
        chk = fix_r if allow_synthetic and store.get_bytes(fix_r) else live_r
        already = _check_existing_published_ready(
            stg, store, chk, stage_root, lock, b, chk == fix_r, now_iso
        )
        if already is not None:
            return already
        raise FileNotFoundError("Missing post_manifest.json; run `av-wrf post` before `publish`")

    window = resolve_cycle_window(b.product, b.output, lock.publish_origin_utc)
    pm = validate_post_manifest(json.loads(pm_raw.decode("utf-8")), b, window)
    is_synth = pm.execution_mode == "synthetic"
    if is_synth and not allow_synthetic:
        raise ValueError("Refusing to publish synthetic forecast to live upstream store")
    if not is_synth:
        verify_solver_receipt(stg, lock, None)
    pub_root = (
        synthetic_cycle_prefix(lock.model_id, lock.cycle_partition)
        if is_synth
        else published_cycle_prefix(lock.model_id, lock.cycle_partition)
    )
    ready_key = f"{pub_root}/_ready.json"
    already = _check_existing_published_ready(
        stg, store, ready_key, stage_root, lock, b, is_synth, now_iso
    )
    if already is not None:
        return already
    verify_staged_objects(stg, stage_root, pm, b, window)
    _promote_verified_objects(stg, store, stage_root, pub_root, pm, lock.attempt_id)
    receipt = _build_ready_receipt(lock, pm, b, window, is_synth)
    put_idempotent(store, ready_key, json_bytes(receipt.to_doc()))
    store.delete_prefix(f"{pub_root}/_promotion_owner.json")
    if not is_synth:
        _advance_latest_pointer_with_retry(store, receipt, b, now_iso)
    stg.delete_prefix(stage_root)
    return receipt


def load_ready_receipt(store: ObjectStore, model_id: str, cycle_partition: str) -> ReadyReceipt:
    """Load an immutable ReadyReceipt from ObjectStore for webhook reconciliation."""
    raw = store.get_bytes(f"{published_cycle_prefix(model_id, cycle_partition)}/_ready.json")
    if raw is None:
        raise FileNotFoundError(f"Missing _ready.json for {model_id} {cycle_partition}")
    doc = json.loads(raw.decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("Expected JSON object in _ready.json")
    return ReadyReceipt.from_doc(doc)


def load_product_ready_receipt(
    config_root: Path,
    store: ObjectStore,
    *,
    product_id: str,
    cycle_partition: str,
    bundle: LoadedBundle | None = None,
) -> ReadyReceipt:
    """Resolve product_id -> model_id inside runner and load the published ReadyReceipt."""
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    return load_ready_receipt(store, b.product.model_id, cycle_partition)
