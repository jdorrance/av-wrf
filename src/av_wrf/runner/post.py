"""Attempt-staged Zarr v3 `post` executor streaming lightweight f000 + one WRF lead at a time."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Literal

import numpy as np

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
    select_cycle_wrfout_slices,
    zarr_shard_relpath,
)
from av_wrf.contracts import (
    WRF_ZARR_V3_2D_NAMES,
    WRF_ZARR_V3_3D_NAMES,
    CycleWindow,
    DiagnosticsReceipt,
    RunLock,
    StagedPostManifest,
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
from av_wrf.runner.stages import _verify_producer_sha256, load_run_lock
from av_wrf.runner.store import (
    ObjectStore,
    attempt_stage_prefix,
    json_bytes,
    post_manifest_key,
    put_idempotent,
)

type _StreamResult = tuple[
    list[tuple[str, str]],
    tuple[float, float, float, float],
    list[float],
]


def _write_frame_shards(
    store: ObjectStore,
    zarr_stage: str,
    time_index: int,
    frame: NormalizedWrfFrame,
    b: LoadedBundle,
    digests: list[tuple[str, str]],
) -> None:
    for name in WRF_ZARR_V3_2D_NAMES:
        if name == "terrain_m":
            continue
        rel = zarr_shard_relpath(name, b.output, time_index=time_index)
        raw = encode_2d_shard_bytes([frame.fields_2d[name]], b.output)
        digests.append((rel, put_idempotent(store, f"{zarr_stage}/{rel}", raw)))
    for name in WRF_ZARR_V3_3D_NAMES:
        rel = zarr_shard_relpath(name, b.output, time_index=time_index)
        raw = encode_3d_shard_bytes([frame.fields_3d[name]], b.output)
        digests.append((rel, put_idempotent(store, f"{zarr_stage}/{rel}", raw)))


def _frame_window_totals(frame: NormalizedWrfFrame) -> tuple[float, float, float, float]:
    dt_h = float(frame.window_end_unix_s - frame.window_start_unix_s) / 3600.0
    sh_m, lh_m, pr_m = (
        float(np.mean(np.frombuffer(frame.fields_2d[key].buf, np.float32), dtype=np.float64))
        for key in ("sensible_heat_flux_wm2", "latent_heat_flux_wm2", "precip_mm_hr")
    )
    return sh_m * dt_h, lh_m * dt_h, pr_m * dt_h, dt_h


def _stream_staged_frames(
    store: ObjectStore,
    zarr_stage: str,
    f000_step: RawWrfBaselineInput | RawWrfStepInput,
    lead_steps: Iterable[RawWrfStepInput],
    window: CycleWindow,
    b: LoadedBundle,
) -> _StreamResult:
    origin_s = int(parse_iso_utc(window.publish_origin_utc).timestamp())
    if (f000_step.valid_time_utc, f000_step.valid_time_unix_s) != (
        window.publish_origin_utc,
        origin_s,
    ):
        raise ValueError("f000 valid_time_utc does not match publish_origin_utc")
    valid_s = tuple(origin_s + h * 3600 for h in window.lead_hours)
    starts_s = (origin_s, *valid_s[:-1])
    base = build_accum_baseline(f000_step, b.domain.bdy_trim_cells)
    digests: list[tuple[str, str]] = []
    dps_series: list[float] = []
    sh_wh, lh_wh, total_h, pr_tot, max_cfl = 0.0, 0.0, 0.0, 0.0, 0.0
    prev_psfc: Sequence[float] = crop_sponge_2d(f000_step.psfc_pa, b.domain.bdy_trim_cells).buf
    scheduled = zip(window.valid_times_utc, starts_s, valid_s, lead_steps, strict=True)
    for i, (expected_time, start_s, end_s, raw_step) in enumerate(scheduled):
        if (raw_step.valid_time_utc, raw_step.valid_time_unix_s) != (expected_time, end_s):
            raise ValueError(f"WRF step timestamp does not match scheduled {expected_time}")
        if raw_step.terrain_m != f000_step.terrain_m:
            raise ValueError(f"WRF HGT changed since f000 at {expected_time}")
        frame, base = post_frame(base, raw_step, b.domain, b.science)
        if (frame.window_start_unix_s, frame.window_end_unix_s) != (start_s, end_s):
            raise ValueError(f"Unexpected accumulation window for {expected_time}")
        max_cfl = max(max_cfl, validate_frame_columns(frame, b.domain))
        if i == 0:
            rel_t = zarr_shard_relpath("terrain_m", b.output)
            raw_t = encode_2d_shard_bytes([frame.fields_2d["terrain_m"]], b.output)
            digests.append((rel_t, put_idempotent(store, f"{zarr_stage}/{rel_t}", raw_t)))
        d_sh, d_lh, d_pr, dt_h = _frame_window_totals(frame)
        sh_wh, lh_wh, pr_tot, total_h = sh_wh + d_sh, lh_wh + d_lh, pr_tot + d_pr, total_h + dt_h
        if i < 6:
            psfc = frame.fields_2d["surface_pressure_pa"].buf
            dps_series.append(round(pressure_tendency_abs_hpa_hr(prev_psfc, psfc, dt_h), 4))
            prev_psfc = psfc
        _write_frame_shards(store, zarr_stage, i, frame, b, digests)
    coords = encode_coord_chunks(b.domain, window.lead_hours, valid_s, starts_s, valid_s)
    for c_rel, c_bytes in sorted(coords.items()):
        digests.append((c_rel, put_idempotent(store, f"{zarr_stage}/{c_rel}", c_bytes)))
    stats = (sh_wh / total_h, lh_wh / total_h, pr_tot, max_cfl)
    return digests, stats, dps_series


def _same_diagnostics_physics(existing_raw: bytes, candidate_raw: bytes) -> bool:
    o = DiagnosticsReceipt.from_doc(json.loads(existing_raw.decode("utf-8"))).to_doc()
    n = DiagnosticsReceipt.from_doc(json.loads(candidate_raw.decode("utf-8"))).to_doc()
    o.pop("stage_seconds", None)
    n.pop("stage_seconds", None)
    return o == n


def _stage_diagnostics_and_manifest(
    store: ObjectStore,
    stage_root: str,
    lk: RunLock,
    window: CycleWindow,
    meta_digests: list[tuple[str, str]],
    streamed: _StreamResult,
    elapsed_s: float,
    execution_mode: str,
) -> None:
    shard_digests, (sh_m, lh_m, pr_tot, max_cfl), dps = streamed
    diag = DiagnosticsReceipt(
        lk.model_id,
        lk.cycle_partition,
        lk.attempt_id,
        (("post", elapsed_s),),
        max_cfl,
        tuple(dps),
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
    pm = StagedPostManifest(
        "forecast.zarr",
        window.lead_hours,
        window.valid_times_utc,
        tuple(meta_digests),
        tuple(shard_digests),
        diag_sha,
        execution_mode=execution_mode,
        producer_sha256=lk.producer_sha256,
    )
    pm_key = post_manifest_key(lk.product_id, lk.cycle_partition, lk.attempt_id)
    put_idempotent(store, pm_key, json_bytes(pm.to_doc()))


def _stage_cycle(
    store: ObjectStore,
    b: LoadedBundle,
    lk: RunLock,
    window: CycleWindow,
    f000_step: RawWrfBaselineInput | RawWrfStepInput,
    lead_steps: Iterable[RawWrfStepInput],
    execution_mode: Literal["solver", "synthetic"],
) -> str:
    stage_root = attempt_stage_prefix(lk.product_id, lk.cycle_partition, lk.attempt_id)
    zarr_stage = f"{stage_root}/forecast.zarr"
    with store.cycle_lock(stage_root):
        t0 = time.perf_counter()
        meta_map = build_zarr_v3_store_metadata(b.domain, b.output, window.lead_hours)
        meta_digests = [
            (rel, put_idempotent(store, f"{zarr_stage}/{rel}", json_bytes(doc)))
            for rel, doc in sorted(meta_map.items())
        ]
        streamed = _stream_staged_frames(store, zarr_stage, f000_step, lead_steps, window, b)
        elapsed_s = round(max(1e-4, time.perf_counter() - t0), 4)
        _stage_diagnostics_and_manifest(
            store, stage_root, lk, window, meta_digests, streamed, elapsed_s, execution_mode
        )
    return zarr_stage


def _open_cycle_inputs(
    store: ObjectStore,
    b: LoadedBundle,
    lk: RunLock,
    win: CycleWindow,
    wrfout_dir: Path | None,
    synthetic: bool,
) -> tuple[Literal["solver", "synthetic"], RawWrfBaselineInput, Iterable[RawWrfStepInput]]:
    if synthetic:
        if wrfout_dir is not None:
            raise ValueError("Pass either --wrfout-dir or --synthetic to `av-wrf post`")
        origin_s = int(parse_iso_utc(win.publish_origin_utc).timestamp())
        baseline = build_synthetic_wrf_baseline(b.domain, win.publish_origin_utc, origin_s)
        steps = (
            build_synthetic_wrf_step(b.domain, valid_time, origin_s + 3600 * hour, hour)
            for hour, valid_time in zip(win.lead_hours, win.valid_times_utc, strict=True)
        )
        return "synthetic", baseline, steps
    if wrfout_dir is None:
        raise ValueError("Pass --wrfout-dir <dir> or --synthetic to `av-wrf post`")
    (f0_p, f0_i), leads = select_cycle_wrfout_slices(
        wrfout_dir, win.publish_origin_utc, win.valid_times_utc
    )
    verify_solver_receipt(
        store, lk, wrfout_dir, selected_wrfouts={f0_p, *(path for path, _ in leads)}
    )
    f000_nc = read_wrfout_baseline_nc(
        f0_p,
        b.domain,
        time_index=f0_i,
        expected_valid_utc=win.publish_origin_utc,
        science=b.science,
    )
    steps_nc = (
        read_wrfout_step_nc(
            path,
            b.domain,
            b.science,
            time_index=index,
            expected_valid_utc=vt,
        )
        for (path, index), vt in zip(leads, win.valid_times_utc, strict=True)
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
    """Stage Zarr from verified solver output or an explicitly synthetic fixture."""
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    lk = load_run_lock(store, product_id, cycle_partition, attempt_id, bundle=b)
    _verify_producer_sha256(lk)
    win = resolve_cycle_window(b.product, b.output, lk.publish_origin_utc)
    mode, f0, st = _open_cycle_inputs(store, b, lk, win, wrfout_dir, synthetic)
    return _stage_cycle(store, b, lk, win, f0, st, mode), len(win.lead_hours)
