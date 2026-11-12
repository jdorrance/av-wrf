"""Vectorized stateful WRF -> Published-Grid Zarr slab post-processing (`post_frame`)."""

from __future__ import annotations

import numpy as np

from av_wrf.contracts import (
    AccumBaseline,
    DomainProfile,
    NormalizedWrfFrame,
    PubBucket2D,
    PubMass2D,
    RawWrfBaselineInput,
    RawWrfStepInput,
    ScienceProfile,
    Slab2DF32,
)
from av_wrf.core.grid import (
    crop_bucket_2d,
    crop_sponge_2d,
    published_grid_shape,
)
from av_wrf.core.post_3d import (
    ArrF64,
    SharedSurface,
    as_f64,
    crop2_f64,
    groom_2d,
    post_3d_slabs,
)
from av_wrf.core.thermo import (
    clamp_spfh_2m_vec,
    groom_f32_buf,
    mixing_ratio_to_spfh_vec,
    parameterized_gust_10m_vec,
    rotate_grid_to_earth_vec,
    validate_thermodynamic_field,
)


def build_accum_baseline(raw: RawWrfBaselineInput | RawWrfStepInput, trim: int) -> AccumBaseline:
    """Crop signed heat-flux accumulators and rainfall buckets onto Published Grid."""
    if bool(np.any(crop2_f64(raw.psfc_pa, trim, "baseline.psfc_pa") <= 0.0)):
        raise ValueError("Non-positive surface pressure in accumulator baseline")
    return _crop_accum_baseline(raw, trim)


def _crop_accum_baseline(raw: RawWrfBaselineInput | RawWrfStepInput, trim: int) -> AccumBaseline:
    achfx, aclhf = crop_sponge_2d(raw.achfx_j_m2, trim), crop_sponge_2d(raw.aclhf_j_m2, trim)
    rainc, rainnc = crop_sponge_2d(raw.rainc_mm, trim), crop_sponge_2d(raw.rainnc_mm, trim)
    i_rc, i_rn = crop_bucket_2d(raw.i_rainc, trim), crop_bucket_2d(raw.i_rainnc, trim)
    return AccumBaseline(
        raw.valid_time_utc, raw.valid_time_unix_s, achfx, aclhf, rainc, i_rc, rainnc, i_rn
    )


def _surface_accum_rates(
    prev: AccumBaseline, curr: AccumBaseline, bucket_mm: float
) -> tuple[ArrF64, ArrF64, ArrF64]:
    dt_s = float(curr.valid_time_unix_s - prev.valid_time_unix_s)
    shp = (prev.achfx_j_m2.ny, prev.achfx_j_m2.nx)
    sh = (as_f64(curr.achfx_j_m2.buf, shp, "achfx") - as_f64(prev.achfx_j_m2.buf, shp)) / dt_s
    lh = (as_f64(curr.aclhf_j_m2.buf, shp, "aclhf") - as_f64(prev.aclhf_j_m2.buf, shp)) / dt_s

    def rain_rate(pv: PubMass2D, pi: PubBucket2D, cv: PubMass2D, ci: PubBucket2D) -> ArrF64:
        p_i = np.frombuffer(pi.buf, dtype=np.int32).reshape(shp)
        c_i = np.frombuffer(ci.buf, dtype=np.int32).reshape(shp)
        if bool(np.any(p_i < 0)) or bool(np.any(c_i < p_i)):
            raise ValueError("Invalid or decreasing cumulative precipitation bucket count")
        d = as_f64(cv.buf, shp, "rain") - as_f64(pv.buf, shp, "rain")
        d += (c_i - p_i) * bucket_mm
        if bool(np.any(d < -1e-6)):
            raise ValueError(f"Negative cumulative precip delta ({float(np.min(d))}) across window")
        return np.maximum(0.0, d) * (3600.0 / dt_s)

    rc = rain_rate(prev.rainc_mm, prev.i_rainc, curr.rainc_mm, curr.i_rainc)
    return sh, lh, rc + rain_rate(prev.rainnc_mm, prev.i_rainnc, curr.rainnc_mm, curr.i_rainnc)


def _post_surface_slabs(
    prev: AccumBaseline,
    curr: AccumBaseline,
    raw: RawWrfStepInput,
    trim: int,
    sc: ScienceProfile,
    shared_2d: SharedSurface,
) -> dict[str, PubMass2D]:
    _, psfc, ca, sa = shared_2d
    ny, nx = psfc.shape
    t2_s = groom_2d(crop2_f64(raw.t2_k, trim, "T2"), 15)
    t2_gr = np.frombuffer(t2_s.buf, np.float32).reshape(ny, nx)
    validate_thermodynamic_field("temp_2m_k", t2_gr)
    t2 = t2_gr.astype(np.float64)
    q2 = mixing_ratio_to_spfh_vec(crop2_f64(raw.q2_kgkg, trim, "Q2"))
    sp2 = clamp_spfh_2m_vec(q2, t2, psfc)
    sh, lh, pr = _surface_accum_rates(prev, curr, sc.bucket_mm)
    u10g = crop2_f64(raw.u10_grid_mps, trim, "U10")
    v10g = crop2_f64(raw.v10_grid_mps, trim, "V10")
    ue, ve = rotate_grid_to_earth_vec(u10g, v10g, ca, sa)
    pblh = crop2_f64(raw.pblh_m, trim, "PBLH", nonnegative=True)
    cape = crop2_f64(raw.afwa_cape_jkg, trim, "AFWA_CAPE_MU", nonnegative=True)
    gust = parameterized_gust_10m_vec(
        ue,
        ve,
        crop2_f64(raw.ustar_mps, trim, "UST"),
        crop2_f64(raw.hfx_wm2, trim, "HFX"),
        crop2_f64(raw.lh_wm2, trim, "LH"),
        pblh,
        t2,
        sp2,
        psfc,
    )
    pairs = (
        ("spfh_2m_kgkg", groom_f32_buf(sp2, 15)),
        ("sensible_heat_flux_wm2", groom_f32_buf(sh, 15)),
        ("latent_heat_flux_wm2", groom_f32_buf(lh, 15)),
        ("precip_mm_hr", groom_f32_buf(pr, 15)),
        ("u_10m_earth_mps", groom_f32_buf(ue, 15)),
        ("v_10m_earth_mps", groom_f32_buf(ve, 15)),
        ("gust_10m_mps", groom_f32_buf(gust, 15)),
    )
    return {
        "temp_2m_k": t2_s,
        "pbl_height_m": groom_2d(pblh, 15),
        "cape_jkg": groom_2d(cape, 15),
        **{nm: PubMass2D(Slab2DF32(ny=ny, nx=nx, buf=b)) for nm, b in pairs},
    }


def post_frame(
    prev: AccumBaseline, raw: RawWrfStepInput, domain: DomainProfile, science: ScienceProfile
) -> tuple[NormalizedWrfFrame, AccumBaseline]:
    """Transform a finite WRF step and baseline into published model and surface fields."""
    if raw.valid_time_unix_s <= prev.valid_time_unix_s:
        raise ValueError("raw.valid_time_unix_s must strictly exceed prev.valid_time_unix_s")
    trim = domain.bdy_trim_cells
    hgt = groom_2d(crop2_f64(raw.terrain_m, trim, "HGT"), 17)
    psfc = groom_2d(crop2_f64(raw.psfc_pa, trim, "PSFC"), 17)
    validate_thermodynamic_field("surface_pressure_pa", np.frombuffer(psfc.buf, np.float32))
    shape = (hgt.ny, hgt.nx)
    if shape != published_grid_shape(domain):
        raise ValueError("Cropped Published Grid shape mismatch")
    shared_2d = (
        as_f64(hgt.buf, shape, "HGT"),
        as_f64(psfc.buf, shape, "PSFC"),
        crop2_f64(raw.cos_alpha, trim, "COSALPHA"),
        crop2_f64(raw.sin_alpha, trim, "SINALPHA"),
    )
    f3d, column_fields, (min_dz, max_dz, min_bl, max_cfl) = post_3d_slabs(
        raw, domain, science, shared_2d
    )
    next_base = _crop_accum_baseline(raw, trim)
    f2d = _post_surface_slabs(prev, next_base, raw, trim, science, shared_2d)
    frame = NormalizedWrfFrame(
        raw.valid_time_utc,
        prev.valid_time_unix_s,
        raw.valid_time_unix_s,
        {"terrain_m": hgt, "surface_pressure_pa": psfc, **column_fields, **f2d},
        f3d,
        min_dz,
        max_dz,
        min_bl,
        max_cfl,
    )
    return frame, next_base


__all__ = [
    "NormalizedWrfFrame",
    "RawWrfBaselineInput",
    "RawWrfStepInput",
    "build_accum_baseline",
    "post_frame",
]
