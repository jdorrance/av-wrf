"""Verify canonical Zarr v3 bytes and decoded forecast columns before promotion."""

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
    ReadyReceipt,
    RunLock,
    StagedPostManifest,
    parse_iso_utc,
)
from av_wrf.core import (
    model_column_spacing,
    published_grid_shape,
    validate_column_geometry,
    validate_thermodynamic_field,
)
from av_wrf.runner.bundle import LoadedBundle
from av_wrf.runner.store import ObjectStore, json_bytes, sha256_hex


def validate_post_manifest(
    raw_doc: object, b: LoadedBundle, window: CycleWindow
) -> StagedPostManifest:
    """Validate `post_manifest.json` horizon and shard inventory."""
    if not isinstance(raw_doc, dict):
        raise ValueError("Expected JSON object in post_manifest.json")
    pm = StagedPostManifest.from_doc(raw_doc)
    if pm.zarr_relpath != "forecast.zarr":
        raise ValueError("Expected forecast.zarr in post_manifest.json")
    if pm.lead_hours != window.lead_hours or pm.valid_times_utc != window.valid_times_utc:
        raise ValueError(
            f"Incomplete cycle horizon: got {len(pm.lead_hours)}, expected {len(window.lead_hours)}"
        )
    expected_shards = expected_shard_keys(b.output, len(pm.lead_hours))
    if (
        len(pm.shard_sha256s) != len(expected_shards)
        or {key for key, _ in pm.shard_sha256s} != expected_shards
    ):
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


def _decode_field_shard(rel_key: str, payload: bytes, b: LoadedBundle) -> npt.NDArray[np.float32]:
    field_name = rel_key.split("/", 1)[0]
    ny, nx = published_grid_shape(b.domain)
    nz = b.domain.exported_levels_k if field_name in WRF_ZARR_V3_3D_NAMES else 1
    arr = decode_shard_f32_array(
        payload, shard_time_steps=1, n_frames=1, nz=nz, ny=ny, nx=nx, output=b.output
    )
    if not bool(np.all(np.isfinite(arr))):
        raise ValueError(f"Non-finite values in decoded staged shard {rel_key}")
    validate_thermodynamic_field(field_name, arr)
    return arr


def _verify_pressure(
    rel_key: str, p3_arr: npt.NDArray[np.float32], ps_arr: npt.NDArray[np.float32]
) -> None:
    if bool(np.any(p3_arr <= 0.0)):
        raise ValueError(f"Non-positive pressure in {rel_key}")
    if bool(np.any(p3_arr[:, 1:] >= p3_arr[:, :-1])):
        raise ValueError(f"Non-monotonic 3D pressure_pa in {rel_key}")
    if bool(np.any(p3_arr[:, 0] >= ps_arr[:, 0])):
        raise ValueError(f"Lowest level pressure_pa must be below surface_pressure_pa in {rel_key}")


def _verified_object(store: ObjectStore, key: str, digest: str) -> bytes:
    payload = store.get_bytes(key)
    if payload is None or sha256_hex(payload) != digest:
        raise ValueError(f"Corrupt or missing Zarr object: {key}")
    return payload


def _verify_diagnostics_receipt(
    store: ObjectStore, root: str, lock: RunLock, exp_sha: str | None
) -> bytes:
    key = f"{root}/_diagnostics.json"
    diag_raw = store.get_bytes(key)
    if diag_raw is None:
        raise FileNotFoundError(f"Missing {key}")
    if exp_sha is not None and sha256_hex(diag_raw) != exp_sha:
        raise ValueError(f"Corrupt {key}")
    diag_obj = json.loads(diag_raw)
    if not isinstance(diag_obj, dict):
        raise ValueError("Expected JSON object in _diagnostics.json")
    diag = DiagnosticsReceipt.from_doc(diag_obj)
    if (diag.model_id, diag.cycle_partition, diag.attempt_id) != (
        lock.model_id,
        lock.cycle_partition,
        lock.attempt_id,
    ):
        raise ValueError(f"Diagnostics provenance does not match RunLock: {key}")
    nonnegative = (
        diag.max_cfl,
        diag.total_precip_mm,
        *diag.initial_dps_dt_hpa_hr,
        *(seconds for _, seconds in diag.stage_seconds),
    )
    if not all(math.isfinite(value) and value >= 0.0 for value in nonnegative) or not all(
        math.isfinite(value) for value in (diag.mean_sensible_flux_wm2, diag.mean_latent_flux_wm2)
    ):
        raise ValueError(f"Invalid forecast diagnostics: {key}")
    return diag_raw


def verify_published_objects(
    store: ObjectStore,
    root: str,
    receipt: ReadyReceipt,
    b: LoadedBundle,
    window: CycleWindow,
    lock: RunLock,
) -> None:
    """Check the committed inventory and hashes before acknowledging a publish retry."""
    meta, coords = _expected_canonical_bytes(b, window)
    keys = expected_shard_keys(b.output, len(window.lead_hours)) | meta.keys()
    digests = dict(receipt.shard_sha256s)
    if len(receipt.shard_sha256s) != len(keys) or digests.keys() != keys:
        raise ValueError("Published Zarr inventory differs from the expected store hierarchy")
    _verify_diagnostics_receipt(store, root, lock, None)
    canonical = meta | coords
    for rel_key, digest in digests.items():
        payload = _verified_object(store, f"{root}/{receipt.zarr_relpath}/{rel_key}", digest)
        if rel_key in canonical and payload != canonical[rel_key]:
            raise ValueError(f"Published Zarr metadata or coordinate {rel_key} contradicts RunLock")


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
    diag_raw = _verify_diagnostics_receipt(stage_store, stage_root, lock, pm.diagnostics_sha256)
    meta_expected, coord_expected = _expected_canonical_bytes(b, window)
    if (
        len(pm.meta_sha256s) != len(meta_expected)
        or {key for key, _ in pm.meta_sha256s} != meta_expected.keys()
    ):
        raise ValueError("Staged Zarr inventory differs from the expected store hierarchy")
    staged_zarr = f"{stage_root}/{pm.zarr_relpath}"
    expected_shas = dict((*pm.meta_sha256s, *pm.shard_sha256s))

    def fetch_verified(rel_key: str) -> bytes:
        return _verified_object(stage_store, f"{staged_zarr}/{rel_key}", expected_shas[rel_key])

    for rel_key, expected in (meta_expected | coord_expected).items():
        payload = fetch_verified(rel_key)
        if payload != expected:
            raise ValueError(f"Staged Zarr metadata or coordinate {rel_key} contradicts RunLock")
        on_verified_object(rel_key, payload)
    terrain_key = zarr_shard_relpath("terrain_m", b.output)
    terrain_payload = fetch_verified(terrain_key)
    terrain = _decode_field_shard(terrain_key, terrain_payload, b)[0, 0]
    on_verified_object(terrain_key, terrain_payload)
    for rel_key, _ in pm.shard_sha256s:
        if (
            rel_key in coord_expected
            or rel_key == terrain_key
            or rel_key.startswith("surface_pressure_pa/")
        ):
            continue
        payload = fetch_verified(rel_key)
        values = _decode_field_shard(rel_key, payload, b)
        if rel_key.startswith("pressure_pa/"):
            t_idx = int(rel_key.split("/")[2])
            ps_key = zarr_shard_relpath("surface_pressure_pa", b.output, time_index=t_idx)
            ps_payload = fetch_verified(ps_key)
            _verify_pressure(rel_key, values, _decode_field_shard(ps_key, ps_payload, b))
            on_verified_object(ps_key, ps_payload)
        elif rel_key.startswith("height_msl_m/"):
            agl = np.subtract(np.moveaxis(values, 1, 0), terrain, dtype=np.float64)
            gaps, min_bl = model_column_spacing(agl)
            validate_column_geometry(float(np.min(gaps)), float(np.max(gaps)), min_bl, b.domain)
        on_verified_object(rel_key, payload)
    return diag_raw
