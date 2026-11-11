"""Canonical Zarr v3 metadata, coordinate, ZEP-0002 shard, and pressure verification for publish."""

from __future__ import annotations

import json
import math

import numpy as np
import numpy.typing as npt

from av_wrf.codecs import (
    build_zarr_v3_store_metadata,
    decode_shard_f32_array,
    encode_coord_chunks,
    expected_shard_keys,
)
from av_wrf.contracts import (
    MAX_ADMITTED_CFL,
    WRF_ZARR_V3_3D_NAMES,
    WRF_ZARR_V3_FIELD_NAMES,
    CycleWindow,
    DiagnosticsReceipt,
    StagedPostManifest,
    parse_iso_utc,
)
from av_wrf.core import published_grid_shape
from av_wrf.runner.bundle import LoadedBundle
from av_wrf.runner.store import ObjectStore, json_bytes, sha256_hex

type _ArrF32 = npt.NDArray[np.float32]


def validate_post_manifest(
    raw_doc: object, b: LoadedBundle, window: CycleWindow
) -> StagedPostManifest:
    """Validate `post_manifest.json` horizon, metadata inventory, and 22-field shard inventory."""
    if not isinstance(raw_doc, dict):
        raise ValueError("Expected JSON object in post_manifest.json")
    pm = StagedPostManifest.from_doc(raw_doc)
    if pm.lead_hours != window.lead_hours or pm.valid_times_utc != window.valid_times_utc:
        raise ValueError(
            f"Incomplete cycle horizon: got {len(pm.lead_hours)}, expected {len(window.lead_hours)}"
        )
    expected_meta = set(build_zarr_v3_store_metadata(b.domain, b.output, pm.lead_hours).keys())
    if {k for k, _ in pm.meta_sha256s} != expected_meta:
        raise ValueError("Staged Zarr metadata inventory does not match expected store hierarchy")
    if {k for k, _ in pm.shard_sha256s} != expected_shard_keys(b.output, len(pm.lead_hours)):
        raise ValueError("Staged Zarr shard inventory is incomplete for configured 22 fields")
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


def _verify_staged_field_shard(
    rel_key: str,
    payload: bytes,
    n_leads: int,
    nj: int,
    ni: int,
    b: LoadedBundle,
    psfc_by_shard: dict[int, _ArrF32],
    p3d_by_shard: dict[int, _ArrF32],
) -> None:
    field_name = rel_key.split("/", 1)[0]
    if rel_key.endswith("zarr.json") or field_name not in WRF_ZARR_V3_FIELD_NAMES:
        return
    if field_name == "terrain_m":
        outer_t, n_frames, nz, t_shard = 1, 1, 1, -1
    else:
        t_shard = int(rel_key.split("/")[2])
        outer_t = min(n_leads, b.output.shard_time_steps)
        n_frames = min(outer_t, n_leads - t_shard * outer_t)
        nz = b.domain.exported_levels_k if field_name in WRF_ZARR_V3_3D_NAMES else 1
    arr = decode_shard_f32_array(
        payload, shard_time_steps=outer_t, n_frames=n_frames, nz=nz, ny=nj, nx=ni, output=b.output
    )[:n_frames]
    if not bool(np.all(np.isfinite(arr))):
        raise ValueError(f"Non-finite value in decoded staged shard {rel_key}")
    if field_name == "surface_pressure_pa":
        if bool(np.any(arr <= 0.0)):
            raise ValueError(f"Non-positive surface_pressure_pa in {rel_key}")
        psfc_by_shard[t_shard] = arr[:, 0, :, :]
    elif field_name == "pressure_pa":
        if bool(np.any(arr <= 0.0)) or (nz > 1 and bool(np.any(np.diff(arr, axis=1) >= 0.0))):
            raise ValueError(f"Non-monotonic or non-positive 3D pressure_pa in {rel_key}")
        p3d_by_shard[t_shard] = arr[:, 0, :, :]


def verify_staged_objects(
    stage_store: ObjectStore,
    stage_root: str,
    pm: StagedPostManifest,
    b: LoadedBundle,
    window: CycleWindow,
) -> None:
    """Verify staged diagnostics CFL, canonical metadata/coords, Zstd shards, and pressure order."""
    diag_raw = stage_store.get_bytes(f"{stage_root}/_diagnostics.json")
    if diag_raw is None or sha256_hex(diag_raw) != pm.diagnostics_sha256:
        raise ValueError("Missing or corrupt staged _diagnostics.json before publish")
    diag_obj = json.loads(diag_raw.decode("utf-8"))
    if not isinstance(diag_obj, dict):
        raise ValueError("Expected JSON object in _diagnostics.json")
    diag = DiagnosticsReceipt.from_doc(diag_obj)
    if not math.isfinite(diag.max_cfl) or diag.max_cfl < 0.0 or diag.max_cfl > MAX_ADMITTED_CFL:
        raise ValueError(f"Refusing publish: invalid max_cfl {diag.max_cfl} > {MAX_ADMITTED_CFL}")

    meta_expected, coord_expected = _expected_canonical_bytes(b, window)
    nj, ni = published_grid_shape(b.domain)
    staged_zarr = f"{stage_root}/{pm.zarr_relpath}"
    psfc_by_shard: dict[int, _ArrF32] = {}
    p3d_by_shard: dict[int, _ArrF32] = {}
    for rel_key, expected_sha in (*pm.meta_sha256s, *pm.shard_sha256s):
        payload = stage_store.get_bytes(f"{staged_zarr}/{rel_key}")
        if payload is None or sha256_hex(payload) != expected_sha:
            raise ValueError(f"Corrupt or missing staged Zarr object: {rel_key}")
        if rel_key in meta_expected and payload != meta_expected[rel_key]:
            raise ValueError(f"Staged Zarr metadata {rel_key} contradicts frozen RunLock")
        if rel_key in coord_expected and payload != coord_expected[rel_key]:
            raise ValueError(f"Staged Zarr coordinate {rel_key} contradicts frozen RunLock")
        _verify_staged_field_shard(
            rel_key, payload, len(pm.lead_hours), nj, ni, b, psfc_by_shard, p3d_by_shard
        )
    for t_shard, p0 in p3d_by_shard.items():
        if t_shard in psfc_by_shard and bool(np.any(p0 > psfc_by_shard[t_shard])):
            raise ValueError(f"Lowest level pressure_pa exceeds surface_pressure_pa at {t_shard}")
