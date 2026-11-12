"""Strict full-horizon `publish` promotion with exclusive cycle lock and stage retention."""

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
    parse_iso_utc,
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
    CASConflict,
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


def _advance_latest_pointer_with_retry(
    store: ObjectStore, receipt: ReadyReceipt, b: LoadedBundle, now_utc_iso: str | None
) -> None:
    latest_key = f"upstream/{receipt.model_id}/latest.json"
    ready_key = f"{published_cycle_prefix(receipt.model_id, receipt.cycle_partition)}/_ready.json"
    origin = parse_iso_utc(receipt.publish_origin_utc)
    next_payload = json_bytes(
        {
            "model_id": receipt.model_id,
            "cycle_partition": receipt.cycle_partition,
            "publish_origin_utc": receipt.publish_origin_utc,
            "ready_key": ready_key,
        }
    )
    for _ in range(MAX_CAS_RETRIES):
        current_raw, expected_tok = store.get_with_token(latest_key)
        now = datetime.now(UTC).isoformat() if now_utc_iso is None else now_utc_iso
        if not is_cycle_live_fresh(b.product, b.output, receipt.publish_origin_utc, now):
            return
        if current_raw is not None:
            cur_doc = json.loads(current_raw)
            current_origin = (
                cur_doc.get("publish_origin_utc") if isinstance(cur_doc, dict) else None
            )
            if not isinstance(current_origin, str):
                raise ValueError("Invalid publish origin in latest.json")
            if origin < parse_iso_utc(current_origin) or cur_doc.get("ready_key") == ready_key:
                return
        try:
            store.compare_and_swap(latest_key, next_payload, expected_tok)
            return
        except CASConflict:
            continue
    raise RuntimeError(f"Failed to advance {latest_key} after {MAX_CAS_RETRIES} CAS attempts")


def _read_ready(
    store: ObjectStore, ready_key: str, model_id: str, cycle_partition: str
) -> ReadyReceipt | None:
    raw = store.get_bytes(ready_key)
    if raw is None:
        return None
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError("Corrupt _ready.json in published store")
    receipt = ReadyReceipt.from_doc(doc)
    if (receipt.model_id, receipt.cycle_partition) != (model_id, cycle_partition):
        raise ValueError(f"Published receipt identity does not match {ready_key}")
    return receipt


def _check_existing_published_ready(
    dst_store: ObjectStore, ready_key: str, lock: RunLock
) -> ReadyReceipt | None:
    committed = _read_ready(dst_store, ready_key, lock.model_id, lock.cycle_partition)
    if committed is not None and (
        committed.attempt_id != lock.attempt_id
        or committed.config_sha256 != lock.config_sha256
        or committed.publish_origin_utc != lock.publish_origin_utc
    ):
        raise FileExistsError(
            f"Cycle {lock.cycle_partition} already published by {committed.attempt_id}"
        )
    return committed


def _recover_published_ready(
    store: ObjectStore, lock: RunLock, live_root: str, fixture_root: str, allow_synthetic: bool
) -> tuple[ReadyReceipt, bool]:
    candidates = ((live_root, False), (fixture_root, True))
    conflict = None
    for root, synthetic in candidates:
        if synthetic and not allow_synthetic:
            break
        try:
            receipt = _check_existing_published_ready(store, f"{root}/_ready.json", lock)
        except FileExistsError as exc:
            conflict = conflict or exc
            continue
        if receipt is not None:
            return receipt, synthetic
    if conflict is not None:
        raise conflict
    raise FileNotFoundError(
        "Missing post_manifest.json; run `av-wrf post` to stage or restage this attempt"
    )


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


def _promote_under_cycle_lock(
    stage_store: ObjectStore,
    dst_store: ObjectStore,
    stage_root: str,
    pub_root: str,
    pm: StagedPostManifest,
    pm_sha256: str,
    b: LoadedBundle,
    window: CycleWindow,
    lock: RunLock,
    is_synth: bool,
) -> ReadyReceipt:
    ready_key = f"{pub_root}/_ready.json"
    pub_zarr = f"{pub_root}/{pm.zarr_relpath}"
    with dst_store.cycle_lock(pub_root):
        already = _check_existing_published_ready(dst_store, ready_key, lock)
        if already is not None:
            return already
        owner_key = f"{pub_root}/_promotion_owner.json"
        owner_doc = json_bytes({"attempt_id": lock.attempt_id, "post_manifest_sha256": pm_sha256})
        prior_owner, owner_tok = dst_store.get_with_token(owner_key)
        if prior_owner != owner_doc:
            if prior_owner is not None:
                dst_store.delete_prefix(pub_zarr)
                dst_store.delete_prefix(f"{pub_root}/_diagnostics.json")
            dst_store.compare_and_swap(owner_key, owner_doc, owner_tok)

        def push_verified(rel_key: str, payload: bytes) -> None:
            put_idempotent(dst_store, f"{pub_zarr}/{rel_key}", payload)

        diag_raw = verify_staged_objects(
            stage_store, stage_root, pm, b, window, lock, on_verified_object=push_verified
        )
        put_idempotent(dst_store, f"{pub_root}/_diagnostics.json", diag_raw)
        receipt = _build_ready_receipt(lock, pm, b, window, is_synth)
        put_idempotent(dst_store, ready_key, json_bytes(receipt.to_doc()))
        dst_store.delete_prefix(owner_key)
    return receipt


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
    prune_stage: bool = False,
    now_utc_iso: str | None = None,
) -> ReadyReceipt:
    """Verify RunLock provenance, ZEP-0002 shards & coords, lock cycle, promote, and CAS latest."""
    stg = store if stage_store is None else stage_store
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    lock = load_run_lock(stg, product_id, cycle_partition, attempt_id, bundle=b)
    stage_root = attempt_stage_prefix(product_id, cycle_partition, attempt_id)
    manifest_key = post_manifest_key(product_id, cycle_partition, attempt_id)
    live_root = published_cycle_prefix(lock.model_id, lock.cycle_partition)
    fixture_root = synthetic_cycle_prefix(lock.model_id, lock.cycle_partition)
    # Acquire the attempt lock before any destination lock. Post uses the same attempt lock.
    with stg.cycle_lock(stage_root):
        pm_raw = stg.get_bytes(manifest_key)
        if pm_raw is None:
            receipt, is_synth = _recover_published_ready(
                store, lock, live_root, fixture_root, allow_synthetic
            )
        else:
            window = resolve_cycle_window(b.product, b.output, lock.publish_origin_utc)
            pm = validate_post_manifest(json.loads(pm_raw), b, window)
            if pm.producer_sha256 != lock.producer_sha256:
                raise ValueError("Staged producer SHA-256 differs from the RunLock")
            is_synth = pm.execution_mode == "synthetic"
            if is_synth and not allow_synthetic:
                raise ValueError("Refusing to publish synthetic forecast to live upstream store")
            if not is_synth:
                verify_solver_receipt(stg, lock, None)
            pub_root = fixture_root if is_synth else live_root
            receipt = _promote_under_cycle_lock(
                stg, store, stage_root, pub_root, pm, sha256_hex(pm_raw), b, window, lock, is_synth
            )
        if not is_synth:
            _advance_latest_pointer_with_retry(store, receipt, b, now_utc_iso)
        if prune_stage:
            stg.delete_prefix(manifest_key)
            stg.delete_prefix(stage_root)
    return receipt


def load_ready_receipt(store: ObjectStore, model_id: str, cycle_partition: str) -> ReadyReceipt:
    """Load an immutable ReadyReceipt from ObjectStore for webhook reconciliation."""
    key = f"{published_cycle_prefix(model_id, cycle_partition)}/_ready.json"
    receipt = _read_ready(store, key, model_id, cycle_partition)
    if receipt is None:
        raise FileNotFoundError(f"Missing _ready.json for {model_id} {cycle_partition}")
    return receipt
