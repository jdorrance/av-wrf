"""Canonical Zarr v3 metadata, coordinate, ZEP-0002 index, and paired pressure verification."""

from __future__ import annotations

import json
import math
from collections.abc import Callable

import numpy as np
import numpy.typing as npt

from av_wrf.codecs import (
    build_zarr_v3_store_metadata,
    decode_shard_f32_array,
    encode_coord_chunks,
    expected_shard_keys,
    zarr_shard_relpath,
)
from av_wrf.contracts import (
    WRF_ZARR_V3_3D_NAMES,
    CycleWindow,
    DiagnosticsReceipt,
    RunLock,
    StagedPostManifest,
    parse_iso_utc,
)
from av_wrf.core import published_grid_shape
from av_wrf.runner.bundle import LoadedBundle
from av_wrf.runner.store import ObjectStore, json_bytes, sha256_hex


def validate_post_manifest(
    raw_doc: object, b: LoadedBundle, window: CycleWindow
) -> StagedPostManifest:
    """Validate `post_manifest.json` horizon, window bounds, metadata, and 22-field inventory."""
    if not isinstance(raw_doc, dict):
        raise ValueError("Expected JSON object in post_manifest.json")
    pm = StagedPostManifest.from_doc(raw_doc)
    if pm.zarr_relpath != "forecast.zarr":
        raise ValueError("Expected forecast.zarr in post_manifest.json")
    if pm.lead_hours != window.lead_hours or pm.valid_times_utc != window.valid_times_utc:
        raise ValueError(
            f"Incomplete cycle horizon: got {len(pm.lead_hours)}, expected {len(window.lead_hours)}"
        )
    expected_meta = set(build_zarr_v3_store_metadata(b.domain, b.output, pm.lead_hours))
    expected_shards = expected_shard_keys(b.output, len(pm.lead_hours))
    for items, expected in ((pm.meta_sha256s, expected_meta), (pm.shard_sha256s, expected_shards)):
        if len(items) != len(expected) or {key for key, _ in items} != expected:
            raise ValueError("Staged Zarr inventory differs from the expected store hierarchy")
    return pm


def _expected_canonical_bytes(
    b: LoadedBundle, window: CycleWindow
) -> tuple[dict[str, bytes], dict[str, bytes]]:
    meta_bytes = {
        rel: json_bytes(doc)
        for rel, doc in build_zarr_v3_store_metadata(b.domain, b.output, window.lead_hours).items()
    }
    valid_s = tuple(int(parse_iso_utc(t).timestamp()) for t in window.valid_times_utc)
    starts_s = tuple(int(parse_iso_utc(t).timestamp()) for t in window.window_starts_utc)
    return meta_bytes, encode_coord_chunks(b.domain, window.lead_hours, valid_s, starts_s, valid_s)


def _decode_field_shard(
    rel_key: str, payload: bytes, n_leads: int, b: LoadedBundle
) -> npt.NDArray[np.float32]:
    field_name, _, *indices = rel_key.split("/")
    if field_name == "terrain_m":
        outer_t, n_frames = 1, 1
    else:
        outer_t = min(n_leads, b.output.shard_time_steps)
        n_frames = min(outer_t, n_leads - int(indices[0]) * outer_t)
    ny, nx = published_grid_shape(b.domain)
    nz = b.domain.exported_levels_k if field_name in WRF_ZARR_V3_3D_NAMES else 1
    arr = decode_shard_f32_array(
        payload, shard_time_steps=outer_t, n_frames=n_frames, nz=nz, ny=ny, nx=nx, output=b.output
    )[:n_frames]
    if not bool(np.all(np.isfinite(arr))):
        raise ValueError(f"Non-finite values in decoded staged shard {rel_key}")
    return arr


def _verify_pressure(
    rel_key: str, p3_arr: npt.NDArray[np.float32], ps_arr: npt.NDArray[np.float32]
) -> None:
    if bool(np.any(ps_arr <= 0.0)) or bool(np.any(p3_arr <= 0.0)):
        raise ValueError(f"Non-positive pressure in {rel_key}")
    if bool(np.any(p3_arr[:, 1:] >= p3_arr[:, :-1])):
        raise ValueError(f"Non-monotonic 3D pressure_pa in {rel_key}")
    if bool(np.any(p3_arr[:, 0] > ps_arr[:, 0])):
        raise ValueError(f"Lowest level pressure_pa exceeds surface_pressure_pa in {rel_key}")


def _verify_diagnostics_receipt(
    stage_store: ObjectStore, stage_root: str, exp_sha: str, lock: RunLock
) -> bytes:
    diag_raw = stage_store.get_bytes(f"{stage_root}/_diagnostics.json")
    if diag_raw is None or sha256_hex(diag_raw) != exp_sha:
        raise ValueError("Missing or corrupt staged _diagnostics.json before publish")
    diag_obj = json.loads(diag_raw.decode("utf-8"))
    if not isinstance(diag_obj, dict):
        raise ValueError("Expected JSON object in _diagnostics.json")
    diag = DiagnosticsReceipt.from_doc(diag_obj)
    if (diag.model_id, diag.cycle_partition, diag.attempt_id) != (
        lock.model_id,
        lock.cycle_partition,
        lock.attempt_id,
    ):
        raise ValueError("Staged diagnostics provenance does not match frozen RunLock")
    nonnegative = (diag.max_cfl, diag.total_precip_mm, *diag.initial_dps_dt_hpa_hr)
    if not all(math.isfinite(value) and value >= 0.0 for value in nonnegative) or not all(
        math.isfinite(value) for value in (diag.mean_sensible_flux_wm2, diag.mean_latent_flux_wm2)
    ):
        raise ValueError("Invalid staged forecast diagnostics")
    return diag_raw


def verify_staged_objects(
    stage_store: ObjectStore,
    stage_root: str,
    pm: StagedPostManifest,
    b: LoadedBundle,
    window: CycleWindow,
    lock: RunLock,
    *,
    on_verified_object: Callable[[str, bytes], None],
) -> bytes:
    """Verify canonical staged metadata and decoded fields and hand off each verified object."""
    diag_raw = _verify_diagnostics_receipt(stage_store, stage_root, pm.diagnostics_sha256, lock)
    meta_expected, coord_expected = _expected_canonical_bytes(b, window)
    staged_zarr = f"{stage_root}/{pm.zarr_relpath}"
    expected_shas = dict((*pm.meta_sha256s, *pm.shard_sha256s))

    def fetch_verified(rel_key: str) -> bytes:
        payload = stage_store.get_bytes(f"{staged_zarr}/{rel_key}")
        if payload is None or sha256_hex(payload) != expected_shas[rel_key]:
            raise ValueError(f"Corrupt or missing staged Zarr object: {rel_key}")
        return payload

    for rel_key, expected in (meta_expected | coord_expected).items():
        payload = fetch_verified(rel_key)
        if payload != expected:
            raise ValueError(f"Staged Zarr metadata or coordinate {rel_key} contradicts RunLock")
        on_verified_object(rel_key, payload)
    n_leads = len(pm.lead_hours)
    for rel_key, _ in pm.shard_sha256s:
        if rel_key in coord_expected or rel_key.startswith("surface_pressure_pa/"):
            continue
        payload = fetch_verified(rel_key)
        values = _decode_field_shard(rel_key, payload, n_leads, b)
        if rel_key.startswith("pressure_pa/"):
            t_idx = int(rel_key.split("/")[2]) * b.output.shard_time_steps
            ps_key = zarr_shard_relpath("surface_pressure_pa", b.output, time_index=t_idx)
            ps_payload = fetch_verified(ps_key)
            _verify_pressure(rel_key, values, _decode_field_shard(ps_key, ps_payload, n_leads, b))
            on_verified_object(ps_key, ps_payload)
        on_verified_object(rel_key, payload)
    return diag_raw
