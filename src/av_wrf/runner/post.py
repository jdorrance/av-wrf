"""Attempt-staged Zarr v3 `post` executor streaming one raw WRF lead at a time."""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Sequence
from datetime import timedelta
from pathlib import Path

from av_wrf.codecs import (
    RawWrfStepInput,
    build_synthetic_wrf_step,
    build_zarr_v3_store_metadata,
    encode_2d_shard_bytes,
    encode_3d_shard_bytes,
    encode_coord_chunks,
    read_wrfout_step_nc,
    select_cycle_wrfout_slices,
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
    return (
        (sum(sh_b) / len(sh_b)) * dt_h,
        (sum(lh_b) / len(lh_b)) * dt_h,
        (sum(pr_b) / len(pr_b)) * dt_h,
        dt_h,
        dps,
        ps_b,
    )


def _derive_step_lead_hour(origin_iso: str, valid_iso: str, expected_iso: str) -> int:
    if valid_iso != expected_iso:
        raise ValueError(f"Frame valid_time_utc {valid_iso} != scheduled {expected_iso}")
    delta_s = int((parse_iso_utc(valid_iso) - parse_iso_utc(origin_iso)).total_seconds())
    if delta_s <= 0 or delta_s % 3600 != 0:
        raise ValueError(f"Frame valid_time_utc {valid_iso} is not a positive whole-hour lead")
    return delta_s // 3600


def _stream_staged_frames(
    store: ObjectStore,
    zarr_stage: str,
    f000_step: RawWrfStepInput,
    lead_steps: Iterable[RawWrfStepInput],
    window: CycleWindow,
    b: LoadedBundle,
) -> _StreamResult:
    if f000_step.valid_time_utc != window.publish_origin_utc:
        raise ValueError("f000 valid_time_utc does not match publish_origin_utc")
    n_expected = len(window.lead_hours)
    base = build_accum_baseline(f000_step, b.domain.bdy_trim_cells)
    digests: list[tuple[str, str]] = []
    leads_seen, valid_s, starts_s, ends_s, valid_iso = [], [], [], [], []
    sh_wh, lh_wh, total_h, pr_tot, max_cfl = 0.0, 0.0, 0.0, 0.0, 0.0
    dps_series: list[float] = []
    prev_psfc: Sequence[float] = crop_sponge_2d(f000_step.psfc_pa, b.domain.bdy_trim_cells).buf
    outer_t = min(n_expected, b.output.shard_time_steps)
    batch: list[NormalizedWrfFrame] = []
    batch_start, seen = 0, 0
    for raw_step in lead_steps:
        if seen >= n_expected:
            raise ValueError(f"Received more than {n_expected} lead steps")
        leads_seen.append(
            _derive_step_lead_hour(
                window.publish_origin_utc, raw_step.valid_time_utc, window.valid_times_utc[seen]
            )
        )
        frame, base = post_frame(base, raw_step, b.domain, b.science)
        max_cfl = max(max_cfl, validate_frame_columns(frame, b.domain))
        if seen == 0:
            rel_t = zarr_shard_relpath("terrain_m", b.output)
            raw_t = encode_2d_shard_bytes([frame.fields_2d["terrain_m"]], b.output)
            digests.append((rel_t, put_idempotent(store, f"{zarr_stage}/{rel_t}", raw_t)))
        d_sh, d_lh, d_pr, dt_h, dps, prev_psfc = _frame_window_totals(frame, prev_psfc)
        sh_wh, lh_wh, pr_tot, total_h = sh_wh + d_sh, lh_wh + d_lh, pr_tot + d_pr, total_h + dt_h
        dps_series.append(dps)
        valid_iso.append(frame.valid_time_utc)
        valid_s.append(frame.window_end_unix_s)
        starts_s.append(frame.window_start_unix_s)
        ends_s.append(frame.window_end_unix_s)
        batch.append(frame)
        seen += 1
        if len(batch) == outer_t or seen == n_expected:
            _flush_shard_batch(store, zarr_stage, batch_start, outer_t, batch, b, digests)
            batch.clear()
            batch_start = seen
    if seen != n_expected:
        raise ValueError(f"Expected {n_expected} lead steps, received {seen}")
    leads_out = tuple(leads_seen)
    coords = encode_coord_chunks(
        b.domain, leads_out, tuple(valid_s), tuple(starts_s), tuple(ends_s)
    )
    for c_rel, c_bytes in sorted(coords.items()):
        digests.append((c_rel, put_idempotent(store, f"{zarr_stage}/{c_rel}", c_bytes)))
    summary = (sh_wh / total_h, lh_wh / total_h, pr_tot, max_cfl)
    return digests, leads_out, tuple(valid_iso), summary, dps_series


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
) -> None:
    shard_digests, leads_out, valid_iso, (sh_m, lh_m, pr_tot, max_cfl), dps = streamed
    diag = DiagnosticsReceipt(
        model_id=lk.model_id,
        cycle_partition=lk.cycle_partition,
        attempt_id=lk.attempt_id,
        stage_seconds=(("post", elapsed_s),),
        max_cfl=max_cfl,
        initial_dps_dt_hpa_hr=tuple(dps[:6]),
        mean_sensible_flux_wm2=round(sh_m, 4),
        mean_latent_flux_wm2=round(lh_m, 4),
        total_precip_mm=round(pr_tot, 4),
    )
    diag_sha = put_idempotent(
        store,
        f"{stage_root}/_diagnostics.json",
        json_bytes(diag.to_doc()),
        equiv=_same_diagnostics_physics,
    )
    manifest = StagedPostManifest(
        zarr_relpath=zarr_rel,
        lead_hours=leads_out,
        valid_times_utc=valid_iso,
        meta_sha256s=tuple(meta_digests),
        shard_sha256s=tuple(shard_digests),
        diagnostics_sha256=diag_sha,
    )
    put_idempotent(
        store,
        post_manifest_key(lk.product_id, lk.cycle_partition, lk.attempt_id),
        json_bytes(manifest.to_doc()),
    )


def post_cycle_store(
    config_root: Path,
    store: ObjectStore,
    *,
    product_id: str,
    cycle_partition: str,
    attempt_id: str,
    f000_step: RawWrfStepInput,
    lead_steps: Iterable[RawWrfStepInput],
    bundle: LoadedBundle | None = None,
    lock: RunLock | None = None,
) -> str:
    """Stream f000 + lead frames into an attempt-scoped sharded Zarr v3 store in ObjectStore."""
    t0 = time.perf_counter()
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    lk = load_run_lock(store, product_id, cycle_partition, attempt_id) if lock is None else lock
    if lk.config_sha256 != b.config_sha256:
        raise ValueError("Config bundle SHA-256 drifted from frozen RunLock")
    window = resolve_cycle_window(b.product, b.output, lk.publish_origin_utc)
    zarr_rel = "forecast.zarr"
    stage_root = attempt_stage_prefix(product_id, cycle_partition, attempt_id)
    zarr_stage = f"{stage_root}/{zarr_rel}"
    meta_digests = [
        (doc_rel, put_idempotent(store, f"{zarr_stage}/{doc_rel}", json_bytes(doc_obj)))
        for doc_rel, doc_obj in sorted(
            build_zarr_v3_store_metadata(b.domain, b.output, window.lead_hours).items()
        )
    ]
    streamed = _stream_staged_frames(store, zarr_stage, f000_step, lead_steps, window, b)
    elapsed_s = round(max(1e-4, time.perf_counter() - t0), 4)
    _stage_diagnostics_and_manifest(
        store, stage_root, lk, zarr_rel, meta_digests, streamed, elapsed_s
    )
    return zarr_stage


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
    if synthetic:
        orig_dt = parse_iso_utc(lk.publish_origin_utc)
        f000 = build_synthetic_wrf_step(
            b.domain, lk.publish_origin_utc, int(orig_dt.timestamp()), 0
        )
        steps: Iterable[RawWrfStepInput] = (
            build_synthetic_wrf_step(
                b.domain,
                format_iso_utc(orig_dt + timedelta(hours=h)),
                int((orig_dt + timedelta(hours=h)).timestamp()),
                h,
            )
            for h in window.lead_hours
        )
    elif wrfout_dir is not None:
        (f0_p, f0_i), lead_slices = select_cycle_wrfout_slices(
            wrfout_dir, window.publish_origin_utc, window.valid_times_utc
        )
        f000 = read_wrfout_step_nc(
            f0_p, b.domain, b.science, time_index=f0_i, expected_valid_utc=window.publish_origin_utc
        )
        steps = (
            read_wrfout_step_nc(lp, b.domain, b.science, time_index=li, expected_valid_utc=vt)
            for (lp, li), vt in zip(lead_slices, window.valid_times_utc, strict=True)
        )
    else:
        raise ValueError("Pass --wrfout-dir <dir> or --synthetic to `av-wrf post`")
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
    )
    return prefix, len(window.lead_hours)
