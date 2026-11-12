"""Strict full-horizon `publish` promotion with exclusive cycle lock and stage retention."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

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
from av_wrf.runner.publish_verify import (
    validate_post_manifest,
    verify_published_objects,
    write_verified_payload,
)
from av_wrf.runner.s3 import S3Store
from av_wrf.runner.solver import verify_solver_receipt
from av_wrf.runner.stages import load_run_lock, verify_producer_sha256
from av_wrf.runner.store import (
    CASConflict,
    LocalFileStore,
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
_FIXTURE = "synthetic-fixture"
_FORECAST = "forecast.zarr"
type _LatestOutcome = Literal["advanced", "current", "newer", "outside_live_window", "fixture"]


def _latest_pointer(raw: bytes, model_id: str) -> tuple[datetime, str]:
    doc = json.loads(raw)
    origin = doc.get("publish_origin_utc") if isinstance(doc, dict) else None
    if not isinstance(origin, str):
        raise ValueError("Invalid publish origin in latest.json")
    timestamp = parse_iso_utc(origin)
    partition = timestamp.strftime("%Y%m%d/%H")
    key = f"{published_cycle_prefix(model_id, partition)}/_ready.json"
    if (doc.get("model_id"), doc.get("cycle_partition"), doc.get("ready_key")) != (
        model_id,
        partition,
        key,
    ):
        raise ValueError("Invalid cycle identity in latest.json")
    return timestamp, key


def _advance_latest_pointer_with_retry(
    store: ObjectStore, receipt: ReadyReceipt, b: LoadedBundle, now_utc_iso: str | None
) -> _LatestOutcome:
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
            return "outside_live_window"
        if current_raw is not None:
            current, current_key = _latest_pointer(current_raw, receipt.model_id)
            if origin < current:
                store.ensure_durable(latest_key)
                return "newer"
            if origin == current and current_key == ready_key:
                store.ensure_durable(latest_key)
                return "current"
        try:
            store.compare_and_swap(latest_key, next_payload, expected_tok)
            return "advanced"
        except CASConflict:
            continue
    raise RuntimeError(f"Failed to advance {latest_key} after {MAX_CAS_RETRIES} CAS attempts")


def _read_ready(
    store: ObjectStore, root: str, model_id: str, cycle_partition: str, *, synthetic: bool = False
) -> ReadyReceipt | None:
    ready_key = f"{root}/_ready.json"
    raw = store.get_bytes(ready_key)
    if raw is None:
        return None
    doc = json.loads(raw)
    if not isinstance(doc, dict):
        raise ValueError("Corrupt _ready.json in published store")
    receipt = ReadyReceipt.from_doc(doc)
    if (receipt.model_id, receipt.cycle_partition) != (model_id, cycle_partition):
        raise ValueError(f"Published receipt identity does not match {ready_key}")
    if (receipt.schema_id, receipt.schema_version) != (ZARR_SCHEMA_ID, ZARR_SCHEMA_VERSION):
        raise ValueError(f"Unsupported publication schema at {ready_key}")
    ready_forecast_relpath(receipt)
    if (receipt.forcing_profile_id == _FIXTURE, receipt.oci_digest == _FIXTURE) != (
        synthetic,
        synthetic,
    ):
        raise ValueError(f"Published receipt execution mode does not match {ready_key}")
    return receipt


def ready_forecast_relpath(receipt: ReadyReceipt) -> str:
    """Validate the Zarr path relative to its cycle receipt."""
    path = receipt.zarr_relpath
    if path != _FORECAST:
        match = re.fullmatch(
            rf"attempts/([A-Za-z0-9][A-Za-z0-9_.-]*)/[0-9a-f]{{64}}/{re.escape(_FORECAST)}", path
        )
        if match is None or match[1] != receipt.attempt_id:
            raise ValueError("Invalid attempt-scoped forecast path in ready receipt")
    return path


def _check_existing_published_ready(
    store: ObjectStore,
    root: str,
    lock: RunLock,
    b: LoadedBundle,
    window: CycleWindow,
    synthetic: bool,
    pm: StagedPostManifest | None = None,
    pm_sha256: str | None = None,
) -> ReadyReceipt | None:
    committed = _read_ready(store, root, lock.model_id, lock.cycle_partition, synthetic=synthetic)
    if committed is None:
        return None
    if committed.attempt_id != lock.attempt_id:
        raise FileExistsError(
            f"Cycle {lock.cycle_partition} already published by {committed.attempt_id}"
        )
    digests = committed.shard_sha256s if pm is None else (*pm.meta_sha256s, *pm.shard_sha256s)
    path = committed.zarr_relpath
    if path != _FORECAST and pm_sha256 is not None and path.split("/")[2] != pm_sha256:
        raise ValueError("Published forecast path differs from the staged manifest")
    if committed != _build_ready_receipt(lock, b, window, digests, synthetic, path):
        raise ValueError("Published receipt provenance or inventory does not match the RunLock")
    verify_published_objects(store, root, committed, b, window, lock)
    store.ensure_durable(f"{root}/_ready.json")
    return committed


def _recover_published_ready(
    store: ObjectStore,
    stage_store: ObjectStore,
    lock: RunLock,
    b: LoadedBundle,
    window: CycleWindow,
    allow_synthetic: bool,
) -> tuple[ReadyReceipt, bool]:
    conflict = None
    for synthetic in (False, True):
        if synthetic and not allow_synthetic:
            break
        root = _destination_prefix(lock, synthetic)
        try:
            receipt = _check_existing_published_ready(store, root, lock, b, window, synthetic)
        except FileExistsError as exc:
            conflict = conflict or exc
            continue
        if receipt is not None:
            if not synthetic:
                verify_solver_receipt(stage_store, lock, None)
            return receipt, synthetic
    if conflict is not None:
        raise conflict
    raise FileNotFoundError(
        "Missing post_manifest.json; run `av-wrf post` to stage or restage this attempt"
    )


def _destination_prefix(lock: RunLock, synthetic: bool) -> str:
    build_prefix = synthetic_cycle_prefix if synthetic else published_cycle_prefix
    return build_prefix(lock.model_id, lock.cycle_partition)


def _build_ready_receipt(
    lock: RunLock,
    b: LoadedBundle,
    window: CycleWindow,
    digests: tuple[tuple[str, str], ...],
    is_synth: bool,
    zarr_relpath: str,
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
        zarr_relpath,
        grid_meta,
        b.domain.exported_levels_k,
        window.lead_hours,
        window.valid_times_utc,
        window.window_starts_utc,
        window.valid_times_utc,
        tuple(f.name for f in WRF_ZARR_V3_FIELDS),
        digests,
        lock.config_sha256,
        _FIXTURE if is_synth else lock.forcing_profile_id,
        _FIXTURE if is_synth else lock.build_manifest.oci_digest,
    )


def _promote_under_cycle_lock(
    stage_store: ObjectStore,
    dst_store: ObjectStore,
    stage_root: str,
    pm: StagedPostManifest,
    pm_sha256: str,
    b: LoadedBundle,
    window: CycleWindow,
    lock: RunLock,
    is_synth: bool,
) -> ReadyReceipt:
    pub_root = _destination_prefix(lock, is_synth)
    ready_key = f"{pub_root}/_ready.json"
    with dst_store.cycle_lock(pub_root):
        already = _check_existing_published_ready(
            dst_store, pub_root, lock, b, window, is_synth, pm, pm_sha256
        )
        if already is not None:
            return already
        if not is_synth and lock.producer_sha256 is None:
            raise ValueError("Live publication requires a producer SHA-256; plan a new attempt")
        verify_producer_sha256(lock)
        payload_prefix = f"attempts/{lock.attempt_id}/{pm_sha256}"
        local = dst_store if isinstance(dst_store, LocalFileStore) else None
        zarr_relpath = _FORECAST if local is not None else f"{payload_prefix}/{_FORECAST}"
        receipt = _build_ready_receipt(
            lock, b, window, (*pm.meta_sha256s, *pm.shard_sha256s), is_synth, zarr_relpath
        )
        ready_raw = json_bytes(receipt.to_doc())
        if local is not None:
            with local._cycle_writer(pub_root) as write:
                write_verified_payload(stage_store, stage_root, pm, b, window, lock, write)
                write("_ready.json", ready_raw)
        else:
            # A paused writer owns only its payload; the first complete ready PUT wins.
            def write(rel_key: str, payload: bytes) -> None:
                put_idempotent(dst_store, f"{pub_root}/{payload_prefix}/{rel_key}", payload)

            write_verified_payload(stage_store, stage_root, pm, b, window, lock, write)
            put_idempotent(dst_store, ready_key, ready_raw)
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
    on_latest: Callable[[_LatestOutcome], None] | None = None,
) -> ReadyReceipt:
    """Publish a verified cycle and report the latest pointer decision to `on_latest`."""
    stg = store if stage_store is None else stage_store
    if prune_stage and isinstance(stg, S3Store):
        raise ValueError("Cannot safely prune a cloud stage with concurrent writers")
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    lock = load_run_lock(stg, product_id, cycle_partition, attempt_id, bundle=b)
    stage_root = attempt_stage_prefix(product_id, cycle_partition, attempt_id)
    manifest_key = post_manifest_key(product_id, cycle_partition, attempt_id)
    window = resolve_cycle_window(b.product, b.output, lock.publish_origin_utc)
    # Acquire the attempt lock before any destination lock. Post uses the same attempt lock.
    with stg.cycle_lock(stage_root):
        pm_raw = stg.get_bytes(manifest_key)
        if pm_raw is None:
            receipt, is_synth = _recover_published_ready(
                store, stg, lock, b, window, allow_synthetic
            )
        else:
            pm = validate_post_manifest(json.loads(pm_raw), b, window)
            if pm.producer_sha256 != lock.producer_sha256:
                raise ValueError("Staged producer SHA-256 differs from the RunLock")
            is_synth = pm.execution_mode == "synthetic"
            if is_synth and not allow_synthetic:
                raise ValueError("Refusing to publish synthetic forecast to live upstream store")
            if not is_synth:
                verify_solver_receipt(stg, lock, None)
            receipt = _promote_under_cycle_lock(
                stg, store, stage_root, pm, sha256_hex(pm_raw), b, window, lock, is_synth
            )
        latest = (
            "fixture"
            if is_synth
            else _advance_latest_pointer_with_retry(store, receipt, b, now_utc_iso)
        )
        if prune_stage:
            stg.delete_prefix(manifest_key)
            stg.delete_prefix(stage_root)
    if on_latest is not None:
        on_latest(latest)
    return receipt


def load_ready_receipt(store: ObjectStore, model_id: str, cycle_partition: str) -> ReadyReceipt:
    """Load an immutable ReadyReceipt from ObjectStore for webhook reconciliation."""
    root = published_cycle_prefix(model_id, cycle_partition)
    receipt = _read_ready(store, root, model_id, cycle_partition)
    if receipt is None:
        raise FileNotFoundError(f"Missing _ready.json for {model_id} {cycle_partition}")
    if isinstance(store, LocalFileStore):
        store.ensure_durable(f"{root}/_ready.json")
    return receipt


__all__ = ["load_ready_receipt", "publish_cycle_receipt", "ready_forecast_relpath"]
