"""Attempt-staged Zarr v3 `post` executor streaming lightweight f000 + one WRF lead at a time."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Sequence
from datetime import timedelta
from pathlib import Path

from av_wrf.codecs import (
    RawWrfBaselineInput,
    RawWrfStepInput,
    build_synthetic_wrf_baseline,
    build_synthetic_wrf_step,
    build_zarr_v3_store_metadata,
    encode_2d_shard_bytes,
    encode_3d_shard_bytes,
    encode_coord_chunks,
    read_wrfout_baseline_nc,
    read_wrfout_step_nc,
    select_cycle_wrfout_windows,
    zarr_shard_relpath,
)
from av_wrf.contracts import (
    WRF_ZARR_V3_2D_NAMES,
    WRF_ZARR_V3_3D_NAMES,
    CycleWindow,
    DiagnosticsReceipt,
    PubMass2D,
    PubMass3D,
    RunLock,
    StagedPostManifest,
    format_iso_utc,
    parse_iso_utc,
)
from av_wrf.core import (
    NormalizedWrfFrame,
    build_accum_baseline,
    crop_sponge_2d,
    post_frame,
    pressure_tendency_abs_hpa_hr,
    resolve_cycle_window,
    validate_frame_columns,
)
from av_wrf.runner.bundle import LoadedBundle, load_bundle
from av_wrf.runner.solver import verify_solver_receipt
from av_wrf.runner.stages import load_run_lock
from av_wrf.runner.store import (
    ObjectStore,
    attempt_stage_prefix,
    json_bytes,
    post_manifest_key,
    put_idempotent,
)

type _StreamResult = tuple[
    list[tuple[str, str]],
    tuple[int, ...],
    tuple[str, ...],
    tuple[float, float, float, float],
    list[float],
]


def _flush_shard_batch(
    store: ObjectStore,
    zarr_stage: str,
    batch_start_idx: int,
    outer_t: int,
    batch_frames: list[NormalizedWrfFrame],
    b: LoadedBundle,
    digests: list[tuple[str, str]],
) -> None:
    for name in WRF_ZARR_V3_2D_NAMES:
        if name == "terrain_m":
            continue
        slabs: list[PubMass2D] = [f.fields_2d[name] for f in batch_frames]
        rel = zarr_shard_relpath(name, b.output, time_index=batch_start_idx)
        raw = encode_2d_shard_bytes(slabs, b.output, shard_time_steps=outer_t)
        digests.append((rel, put_idempotent(store, f"{zarr_stage}/{rel}", raw)))
    for name in WRF_ZARR_V3_3D_NAMES:
        vols: list[PubMass3D] = [f.fields_3d[name] for f in batch_frames]
        rel = zarr_shard_relpath(name, b.output, time_index=batch_start_idx)
        raw = encode_3d_shard_bytes(vols, b.output, shard_time_steps=outer_t)
        digests.append((rel, put_idempotent(store, f"{zarr_stage}/{rel}", raw)))


def _frame_window_totals(
    frame: NormalizedWrfFrame, prev_psfc: Sequence[float]
) -> tuple[float, float, float, float, float, Sequence[float]]:
    dt_h = float(frame.window_end_unix_s - frame.window_start_unix_s) / 3600.0
    ps_b = frame.fields_2d["surface_pressure_pa"].buf
    dps = round(pressure_tendency_abs_hpa_hr(prev_psfc, ps_b, dt_h), 4)
    sh_b = frame.fields_2d["sensible_heat_flux_wm2"].buf
    lh_b = frame.fields_2d["latent_heat_flux_wm2"].buf
    pr_b = frame.fields_2d["precip_mm_hr"].buf
    sh_wh = (sum(sh_b) / len(sh_b)) * dt_h
    lh_wh = (sum(lh_b) / len(lh_b)) * dt_h
    return sh_wh, lh_wh, (sum(pr_b) / len(pr_b)) * dt_h, dt_h, dps, ps_b


def _derive_step_lead_hour(origin_iso: str, valid_iso: str, expected_iso: str) -> int:
    if valid_iso != expected_iso:
        raise ValueError(f"Frame valid_time_utc {valid_iso} != scheduled {expected_iso}")
    delta_s = int((parse_iso_utc(valid_iso) - parse_iso_utc(origin_iso)).total_seconds())
    if delta_s <= 0 or delta_s % 3600 != 0:
        raise ValueError(f"Frame valid_time_utc {valid_iso} is not a positive whole-hour lead")
    return delta_s // 3600


def _put_terrain_shard(
    store: ObjectStore, zarr_stage: str, frame: NormalizedWrfFrame, b: LoadedBundle
) -> tuple[str, str]:
    rel_t = zarr_shard_relpath("terrain_m", b.output)
    raw_t = encode_2d_shard_bytes([frame.fields_2d["terrain_m"]], b.output)
    return rel_t, put_idempotent(store, f"{zarr_stage}/{rel_t}", raw_t)


def _stream_staged_frames(
    store: ObjectStore,
    zarr_stage: str,
    f000_step: RawWrfBaselineInput | RawWrfStepInput,
    lead_steps: Iterable[RawWrfStepInput],
    window: CycleWindow,
    b: LoadedBundle,
) -> _StreamResult:
    if f000_step.valid_time_utc != window.publish_origin_utc:
        raise ValueError("f000 valid_time_utc does not match publish_origin_utc")
    n_exp = len(window.lead_hours)
    base = build_accum_baseline(f000_step, b.domain.bdy_trim_cells)
    digests: list[tuple[str, str]] = []
    leads_seen, valid_s, starts_s, valid_iso, dps_series = [], [], [], [], []
    sh_wh, lh_wh, total_h, pr_tot, max_cfl = 0.0, 0.0, 0.0, 0.0, 0.0
    prev_psfc: Sequence[float] = crop_sponge_2d(f000_step.psfc_pa, b.domain.bdy_trim_cells).buf
    outer_t = min(n_exp, b.output.shard_time_steps)
    batch: list[NormalizedWrfFrame] = []
    batch_start, seen = 0, 0
    for raw_step in lead_steps:
        if seen >= n_exp:
            raise ValueError(f"Received more than {n_exp} lead steps")
        lead_h = _derive_step_lead_hour(
            window.publish_origin_utc, raw_step.valid_time_utc, window.valid_times_utc[seen]
        )
        leads_seen.append(lead_h)
        frame, base = post_frame(base, raw_step, b.domain, b.science)
        max_cfl = max(max_cfl, validate_frame_columns(frame, b.domain))
        if seen == 0:
            digests.append(_put_terrain_shard(store, zarr_stage, frame, b))
        d_sh, d_lh, d_pr, dt_h, dps, prev_psfc = _frame_window_totals(frame, prev_psfc)
        sh_wh, lh_wh, pr_tot, total_h = sh_wh + d_sh, lh_wh + d_lh, pr_tot + d_pr, total_h + dt_h
        dps_series.append(dps)
        valid_iso.append(frame.valid_time_utc)
        valid_s.append(frame.window_end_unix_s)
        starts_s.append(frame.window_start_unix_s)
        batch.append(frame)
        seen += 1
        if len(batch) == outer_t or seen == n_exp:
            _flush_shard_batch(store, zarr_stage, batch_start, outer_t, batch, b, digests)
            batch.clear()
            batch_start = seen
    if seen != n_exp:
        raise ValueError(f"Expected {n_exp} lead steps, received {seen}")
    leads_out, valid_t = tuple(leads_seen), tuple(valid_s)
    coords = encode_coord_chunks(b.domain, leads_out, valid_t, tuple(starts_s), valid_t)
    for c_rel, c_bytes in sorted(coords.items()):
        digests.append((c_rel, put_idempotent(store, f"{zarr_stage}/{c_rel}", c_bytes)))
    stats = (sh_wh / total_h, lh_wh / total_h, pr_tot, max_cfl)
    return digests, leads_out, tuple(valid_iso), stats, dps_series


def _same_diagnostics_physics(existing_raw: bytes, candidate_raw: bytes) -> bool:
    old = DiagnosticsReceipt.from_doc(json.loads(existing_raw.decode("utf-8")))
    new = DiagnosticsReceipt.from_doc(json.loads(candidate_raw.decode("utf-8")))
    return (
        old.max_cfl == new.max_cfl
        and old.initial_dps_dt_hpa_hr == new.initial_dps_dt_hpa_hr
        and old.mean_sensible_flux_wm2 == new.mean_sensible_flux_wm2
        and old.mean_latent_flux_wm2 == new.mean_latent_flux_wm2
        and old.total_precip_mm == new.total_precip_mm
    )


def _stage_diagnostics_and_manifest(
    store: ObjectStore,
    stage_root: str,
    lk: RunLock,
    zarr_rel: str,
    meta_digests: list[tuple[str, str]],
    streamed: _StreamResult,
    elapsed_s: float,
    execution_mode: str,
) -> None:
    shard_digests, leads_out, valid_iso, (sh_m, lh_m, pr_tot, max_cfl), dps = streamed
    diag = DiagnosticsReceipt(
        lk.model_id,
        lk.cycle_partition,
        lk.attempt_id,
        (("post", elapsed_s),),
        max_cfl,
        tuple(dps[:6]),
        round(sh_m, 4),
        round(lh_m, 4),
        round(pr_tot, 4),
    )
    diag_sha = put_idempotent(
        store,
        f"{stage_root}/_diagnostics.json",
        json_bytes(diag.to_doc()),
        equiv=_same_diagnostics_physics,
    )
    manifest = StagedPostManifest(
        zarr_rel,
        leads_out,
        valid_iso,
        tuple(meta_digests),
        tuple(shard_digests),
        diag_sha,
        execution_mode,
    )
    pm_key = post_manifest_key(lk.product_id, lk.cycle_partition, lk.attempt_id)
    put_idempotent(store, pm_key, json_bytes(manifest.to_doc()))


def post_cycle_store(
    config_root: Path,
    store: ObjectStore,
    *,
    product_id: str,
    cycle_partition: str,
    attempt_id: str,
    f000_step: RawWrfBaselineInput | RawWrfStepInput,
    lead_steps: Iterable[RawWrfStepInput],
    bundle: LoadedBundle | None = None,
    lock: RunLock | None = None,
    execution_mode: str = "solver",
) -> str:
    """Stream f000 + lead frames into an attempt-scoped sharded Zarr v3 store in ObjectStore."""
    if execution_mode not in ("solver", "synthetic"):
        raise ValueError(f"Invalid execution_mode: {execution_mode}")
    t0 = time.perf_counter()
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    lk = load_run_lock(store, product_id, cycle_partition, attempt_id) if lock is None else lock
    if lk.config_sha256 != b.config_sha256:
        raise ValueError("Config bundle SHA-256 drifted from frozen RunLock")
    window = resolve_cycle_window(b.product, b.output, lk.publish_origin_utc)
    zarr_rel = "forecast.zarr"
    stage_root = attempt_stage_prefix(product_id, cycle_partition, attempt_id)
    zarr_stage = f"{stage_root}/{zarr_rel}"
    meta_map = build_zarr_v3_store_metadata(b.domain, b.output, window.lead_hours)
    meta_digests = [
        (rel, put_idempotent(store, f"{zarr_stage}/{rel}", json_bytes(doc)))
        for rel, doc in sorted(meta_map.items())
    ]
    streamed = _stream_staged_frames(store, zarr_stage, f000_step, lead_steps, window, b)
    elapsed_s = round(max(1e-4, time.perf_counter() - t0), 4)
    _stage_diagnostics_and_manifest(
        store, stage_root, lk, zarr_rel, meta_digests, streamed, elapsed_s, execution_mode
    )
    return zarr_stage


def _open_cycle_inputs(
    store: ObjectStore,
    b: LoadedBundle,
    lk: RunLock,
    window: CycleWindow,
    wrfout_dir: Path | None,
    synthetic: bool,
) -> tuple[str, RawWrfBaselineInput | RawWrfStepInput, Iterable[RawWrfStepInput]]:
    if synthetic:
        orig_dt = parse_iso_utc(lk.publish_origin_utc)
        f000 = build_synthetic_wrf_baseline(
            b.domain, lk.publish_origin_utc, int(orig_dt.timestamp())
        )
        steps = (
            build_synthetic_wrf_step(
                b.domain,
                format_iso_utc(orig_dt + timedelta(hours=h)),
                int((orig_dt + timedelta(hours=h)).timestamp()),
                h,
            )
            for h in window.lead_hours
        )
        return "synthetic", f000, steps
    if wrfout_dir is None:
        raise ValueError("Pass --wrfout-dir <dir> or --synthetic to `av-wrf post`")
    verify_solver_receipt(store, lk, wrfout_dir)
    (f0_p, f0_i), lead_windows = select_cycle_wrfout_windows(
        wrfout_dir, window.publish_origin_utc, window.valid_times_utc
    )
    f000_nc = read_wrfout_baseline_nc(
        f0_p, b.domain, time_index=f0_i, expected_valid_utc=window.publish_origin_utc
    )
    steps_nc = (
        read_wrfout_step_nc(
            win[-1][0],
            b.domain,
            b.science,
            time_index=win[-1][1],
            expected_valid_utc=vt,
            window_slices=win,
        )
        for win, vt in zip(lead_windows, window.valid_times_utc, strict=True)
    )
    return "solver", f000_nc, steps_nc


def post_cycle_from_inputs(
    config_root: Path,
    store: ObjectStore,
    *,
    product_id: str,
    cycle_partition: str,
    attempt_id: str,
    wrfout_dir: Path | None,
    synthetic: bool = False,
    bundle: LoadedBundle | None = None,
) -> tuple[str, int]:
    """Stream `post_cycle_store` one lead at a time from `wrfout_d01_*` or synthetic steps."""
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    lk = load_run_lock(store, product_id, cycle_partition, attempt_id)
    window = resolve_cycle_window(b.product, b.output, lk.publish_origin_utc)
    mode, f000, steps = _open_cycle_inputs(store, b, lk, window, wrfout_dir, synthetic)
    prefix = post_cycle_store(
        config_root,
        store,
        product_id=product_id,
        cycle_partition=cycle_partition,
        attempt_id=attempt_id,
        f000_step=f000,
        lead_steps=steps,
        bundle=b,
        lock=lk,
        execution_mode=mode,
    )
    return prefix, len(window.lead_hours)
