"""Strict full-horizon `publish` verifier, ZEP-0002 gate, cheap promoter, and CAS updater."""

from __future__ import annotations

import json
import math
from pathlib import Path

from av_wrf.codecs import (
    build_zarr_v3_store_metadata,
    decode_shard_f32,
    expected_shard_keys,
    verify_shard_index,
)
from av_wrf.contracts import (
    MAX_ADMITTED_CFL,
    WRF_ZARR_V3_3D_NAMES,
    WRF_ZARR_V3_FIELD_NAMES,
    WRF_ZARR_V3_FIELDS,
    ZARR_SCHEMA_ID,
    ZARR_SCHEMA_VERSION,
    CycleWindow,
    DiagnosticsReceipt,
    ReadyReceipt,
    RunLock,
    StagedPostManifest,
)
from av_wrf.core import (
    published_grid_shape,
    published_lambert_grid_metadata,
    resolve_cycle_window,
)
from av_wrf.runner.bundle import LoadedBundle, load_bundle
from av_wrf.runner.stages import load_run_lock
from av_wrf.runner.store import (
    ObjectStore,
    attempt_stage_prefix,
    json_bytes,
    post_manifest_key,
    published_cycle_prefix,
    put_idempotent,
    sha256_hex,
)

MAX_CAS_RETRIES = 8


def _verify_staged_field_shard(
    rel_key: str,
    payload: bytes,
    n_leads: int,
    nj: int,
    ni: int,
    b: LoadedBundle,
    decoded_shapes: set[str],
) -> None:
    field_name = rel_key.split("/", 1)[0]
    if rel_key.endswith("zarr.json") or field_name not in WRF_ZARR_V3_FIELD_NAMES:
        return
    if field_name == "terrain_m":
        outer_t, n_frames, nz, shape_tag = 1, 1, 1, "static_2d"
    else:
        t_shard = int(rel_key.split("/")[2])
        outer_t = min(n_leads, b.output.shard_time_steps)
        n_frames = min(outer_t, n_leads - t_shard * outer_t)
        is_3d = field_name in WRF_ZARR_V3_3D_NAMES
        nz = b.domain.exported_levels_k if is_3d else 1
        shape_tag = "3d" if is_3d else "2d"
    verify_shard_index(
        payload, shard_time_steps=outer_t, n_frames=n_frames, ny=nj, nx=ni, output=b.output
    )
    if shape_tag not in decoded_shapes:
        vals = decode_shard_f32(
            payload,
            shard_time_steps=outer_t,
            n_frames=n_frames,
            nz=nz,
            ny=nj,
            nx=ni,
            output=b.output,
        )
        if any(not math.isfinite(v) for v in vals[: n_frames * nz * nj * ni]):
            raise ValueError(f"Non-finite value in decoded staged shard {rel_key}")
        decoded_shapes.add(shape_tag)


def _verify_and_promote_attempt(
    store: ObjectStore,
    stage_root: str,
    pub_root: str,
    pm: StagedPostManifest,
    b: LoadedBundle,
) -> None:
    diag_key = f"{stage_root}/_diagnostics.json"
    diag_raw = store.get_bytes(diag_key)
    if diag_raw is None or sha256_hex(diag_raw) != pm.diagnostics_sha256:
        raise ValueError("Missing or corrupt staged _diagnostics.json before publish")
    diag_obj = json.loads(diag_raw.decode("utf-8"))
    if not isinstance(diag_obj, dict):
        raise ValueError("Expected JSON object in _diagnostics.json")
    diag = DiagnosticsReceipt.from_doc(diag_obj)
    if diag.max_cfl > MAX_ADMITTED_CFL:
        raise ValueError(f"Refusing publish: max_cfl {diag.max_cfl} > {MAX_ADMITTED_CFL}")

    nj, ni = published_grid_shape(b.domain)
    n_leads = len(pm.lead_hours)
    staged_zarr, pub_zarr = f"{stage_root}/{pm.zarr_relpath}", f"{pub_root}/{pm.zarr_relpath}"
    decoded_shapes: set[str] = set()
    for rel_key, expected_sha in (*pm.meta_sha256s, *pm.shard_sha256s):
        src_key = f"{staged_zarr}/{rel_key}"
        payload = store.get_bytes(src_key)
        if payload is None or sha256_hex(payload) != expected_sha:
            raise ValueError(f"Corrupt or missing staged Zarr object: {rel_key}")
        _verify_staged_field_shard(rel_key, payload, n_leads, nj, ni, b, decoded_shapes)
        store.copy_if_absent(src_key, f"{pub_zarr}/{rel_key}", expected_sha)
    store.copy_if_absent(diag_key, f"{pub_root}/_diagnostics.json", pm.diagnostics_sha256)


def _advance_latest_pointer_with_retry(store: ObjectStore, receipt: ReadyReceipt) -> None:
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
            if receipt.publish_origin_utc < str(cur_doc["publish_origin_utc"]):
                return
            if cur_doc.get("ready_key") == ready_key:
                return
        try:
            store.compare_and_swap(latest_key, next_payload, expected_sha)
            return
        except RuntimeError:
            continue
    raise RuntimeError(f"Failed to advance {latest_key} after {MAX_CAS_RETRIES} CAS attempts")


def _validate_post_manifest(
    raw_doc: object, b: LoadedBundle, window: CycleWindow
) -> StagedPostManifest:
    if not isinstance(raw_doc, dict):
        raise ValueError("Expected JSON object in post_manifest.json")
    pm = StagedPostManifest.from_doc(raw_doc)
    if pm.lead_hours != window.lead_hours or pm.valid_times_utc != window.valid_times_utc:
        raise ValueError(
            f"Incomplete or mismatched cycle horizon: got {len(pm.lead_hours)} leads, "
            f"expected {len(window.lead_hours)} ({window.lead_hours})"
        )
    expected_meta = set(build_zarr_v3_store_metadata(b.domain, b.output, pm.lead_hours).keys())
    if {k for k, _ in pm.meta_sha256s} != expected_meta:
        raise ValueError("Staged Zarr metadata inventory does not match expected store hierarchy")
    if {k for k, _ in pm.shard_sha256s} != expected_shard_keys(b.output, len(pm.lead_hours)):
        raise ValueError("Staged Zarr shard inventory is incomplete for configured 22 fields")
    return pm


def _check_existing_published_ready(
    store: ObjectStore, ready_key: str, stage_root: str, lock: RunLock
) -> ReadyReceipt | None:
    existing_ready = store.get_bytes(ready_key)
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
    _advance_latest_pointer_with_retry(store, committed)
    store.delete_prefix(stage_root)
    return committed


def publish_cycle_receipt(
    config_root: Path,
    store: ObjectStore,
    *,
    product_id: str,
    cycle_partition: str,
    attempt_id: str,
    bundle: LoadedBundle | None = None,
) -> ReadyReceipt:
    """Verify complete RunLock horizon + ZEP-0002, promote store, clean stage, and CAS latest."""
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    lock = load_run_lock(store, product_id, cycle_partition, attempt_id)
    if lock.config_sha256 != b.config_sha256:
        raise ValueError("Config bundle SHA-256 drifted from frozen RunLock")
    stage_root = attempt_stage_prefix(product_id, cycle_partition, attempt_id)
    pub_root = published_cycle_prefix(lock.model_id, lock.cycle_partition)
    ready_key = f"{pub_root}/_ready.json"
    already = _check_existing_published_ready(store, ready_key, stage_root, lock)
    if already is not None:
        return already

    pm_raw = store.get_bytes(post_manifest_key(product_id, cycle_partition, attempt_id))
    if pm_raw is None:
        raise FileNotFoundError("Missing post_manifest.json; run `av-wrf post` before `publish`")
    window = resolve_cycle_window(b.product, b.output, lock.publish_origin_utc)
    pm = _validate_post_manifest(json.loads(pm_raw.decode("utf-8")), b, window)
    _verify_and_promote_attempt(store, stage_root, pub_root, pm, b)
    receipt = ReadyReceipt(
        schema_id=ZARR_SCHEMA_ID,
        schema_version=ZARR_SCHEMA_VERSION,
        model_id=lock.model_id,
        cycle_partition=lock.cycle_partition,
        publish_origin_utc=lock.publish_origin_utc,
        attempt_id=lock.attempt_id,
        zarr_relpath=pm.zarr_relpath,
        grid=published_lambert_grid_metadata(b.domain),
        exported_levels_k=b.domain.exported_levels_k,
        lead_hours=pm.lead_hours,
        valid_times_utc=pm.valid_times_utc,
        window_starts_utc=window.window_starts_utc,
        window_ends_utc=pm.valid_times_utc,
        field_names=tuple(f.name for f in WRF_ZARR_V3_FIELDS),
        shard_sha256s=(*pm.meta_sha256s, *pm.shard_sha256s),
        config_sha256=lock.config_sha256,
        forcing_profile_id=lock.forcing_profile_id,
        oci_digest=lock.build_manifest.oci_digest,
    )
    put_idempotent(store, ready_key, json_bytes(receipt.to_doc()))
    _advance_latest_pointer_with_retry(store, receipt)
    store.delete_prefix(stage_root)
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
