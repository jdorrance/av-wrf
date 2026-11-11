"""Vectorized stateful WRF -> Published-Grid Zarr slab post-processing (`post_frame`)."""

from __future__ import annotations

from array import array

import numpy as np
import numpy.typing as npt

from av_wrf.contracts import (
    BL_CEILING_AGL_M,
    AccumBaseline,
    CompMass2D,
    DomainProfile,
    NormalizedWrfFrame,
    PubBucket2D,
    PubMass2D,
    PubMass3D,
    RawWrfBaselineInput,
    RawWrfStepInput,
    ScienceProfile,
    Slab2DF32,
    Slab3DF32,
)
from av_wrf.core.grid import (
    crop_bucket_2d,
    crop_sponge_2d,
    crop_sponge_3d,
    destagger_x_3d,
    destagger_y_3d,
    destagger_z_3d,
    published_grid_shape,
)
from av_wrf.core.thermo import (
    G0_MPS2,
    KAPPA_DRY,
    P0_PA,
    R_DRY_J_KG_K,
    VIRTUAL_TEMP_COEFF,
    WRF_T0_K,
    clamp_spfh_2m_vec,
    groom_f32_buf,
    hypsometric_mslp_vec,
    parameterized_gust_10m_vec,
)

type _ArrF64 = npt.NDArray[np.float64]


def _as_f64(buf: array[float], shape: tuple[int, ...], label: str = "slab") -> _ArrF64:
    arr = np.frombuffer(buf, dtype=np.float32).reshape(shape).astype(np.float64)
    if not bool(np.all(np.isfinite(arr))):
        raise ValueError(f"Non-finite value encountered in {label}")
    return arr


def _crop2_f64(slab: CompMass2D, trim: int, label: str = "2d") -> _ArrF64:
    cropped = crop_sponge_2d(slab, trim)
    return _as_f64(cropped.buf, (cropped.ny, cropped.nx), label)


def _groom_2d(slab: PubMass2D, keepbits: int, label: str = "2d") -> PubMass2D:
    arr = _as_f64(slab.buf, (slab.ny, slab.nx), label)
    return PubMass2D(Slab2DF32(ny=slab.ny, nx=slab.nx, buf=groom_f32_buf(arr, keepbits)))


def build_accum_baseline(raw: RawWrfBaselineInput | RawWrfStepInput, trim: int) -> AccumBaseline:
    """Crop signed heat-flux accumulators and rainfall buckets onto Published Grid."""
    if bool(np.any(_crop2_f64(raw.psfc_pa, trim, "f000.psfc_pa") <= 0.0)):
        raise ValueError("Non-positive surface pressure in f000 baseline")
    achfx, aclhf = crop_sponge_2d(raw.achfx_j_m2, trim), crop_sponge_2d(raw.aclhf_j_m2, trim)
    rainc, rainnc = crop_sponge_2d(raw.rainc_mm, trim), crop_sponge_2d(raw.rainnc_mm, trim)
    for nm, sl in (("achfx", achfx), ("aclhf", aclhf), ("rainc", rainc), ("rainnc", rainnc)):
        _as_f64(sl.buf, (sl.ny, sl.nx), nm)
    i_rc, i_rn = crop_bucket_2d(raw.i_rainc, trim), crop_bucket_2d(raw.i_rainnc, trim)
    return AccumBaseline(
        raw.valid_time_utc, raw.valid_time_unix_s, achfx, aclhf, rainc, i_rc, rainnc, i_rn
    )


def _compute_cloud_slabs(
    cld_all: PubMass3D, p_groomed: array[float], psfc: PubMass2D, k_exp: int
) -> tuple[array[float], array[float]]:
    plane = psfc.ny * psfc.nx
    c = np.clip(_as_f64(cld_all.buf, (cld_all.nz, plane), "cldfra"), 0.0, 1.0)
    clear = 1.0 - c[0]
    for k in range(1, c.shape[0]):
        prev, curr = c[k - 1], c[k]
        denom = np.where(prev < 0.999999, 1.0 - prev, 1.0)
        ratio = np.clip((1.0 - np.maximum(prev, curr)) / denom, 0.0, 1.0)
        clear = np.where(prev < 0.999999, clear * ratio, 0.0)
    p_2d = _as_f64(p_groomed, (k_exp, plane), "pressure_pa")
    psfc_1d = _as_f64(psfc.buf, (plane,), "surface_pressure_pa")
    clear_low, prev_low = np.ones(plane, dtype=np.float64), np.zeros(plane, dtype=np.float64)
    started = np.zeros(plane, dtype=bool)
    for k in range(k_exp):
        inc, ck = p_2d[k] >= 0.8 * psfc_1d, c[k]
        denom = np.where(prev_low < 0.999999, 1.0 - prev_low, 1.0)
        ratio = np.clip((1.0 - np.maximum(prev_low, ck)) / denom, 0.0, 1.0)
        step = np.where(prev_low < 0.999999, clear_low * ratio, 0.0)
        clear_low = np.where(inc, np.where(started, step, 1.0 - ck), clear_low)
        prev_low, started = np.where(inc, ck, prev_low), started | inc
    tot_b = groom_f32_buf(100.0 * (1.0 - np.clip(clear, 0.0, 1.0)), 15)
    return tot_b, groom_f32_buf(100.0 * (1.0 - np.clip(clear_low, 0.0, 1.0)), 15)


def _validate_and_groom_3d_pressure(
    raw: RawWrfStepInput, psfc_2d: _ArrF64, trim: int, k_exp: int, shp3: tuple[int, int, int]
) -> tuple[_ArrF64, array[float]]:
    p_pa = _as_f64(crop_sponge_3d(raw.p_pert_pa, trim, k_exp).buf, shp3, "P") + _as_f64(
        crop_sponge_3d(raw.pb_pa, trim, k_exp).buf, shp3, "PB"
    )
    if bool(np.any(p_pa <= 0.0)) or bool(np.any(psfc_2d <= 0.0)):
        raise ValueError("Non-positive surface or 3D pressure in post_frame")
    if bool(np.any(p_pa[0] > psfc_2d)):
        raise ValueError("Lowest model-level pressure exceeds surface_pressure_pa")
    if k_exp > 1 and bool(np.any(np.diff(p_pa, axis=0) >= 0.0)):
        raise ValueError("Non-monotonic 3D column pressure (pressure must decrease with height)")
    p_buf = groom_f32_buf(p_pa, 17)
    p_gr = _as_f64(p_buf, shp3, "pressure_pa")
    if bool(np.any(p_gr[0] > psfc_2d)) or (
        k_exp > 1 and bool(np.any(np.diff(p_gr, axis=0) >= 0.0))
    ):
        raise ValueError("Bit-groomed 3D column pressure violated monotonic ordering")
    return p_pa, p_buf


def _post_3d_slabs(
    raw: RawWrfStepInput,
    domain: DomainProfile,
    science: ScienceProfile,
    shared_2d: tuple[PubMass2D, PubMass2D, PubMass2D, PubMass2D],
) -> tuple[dict[str, PubMass3D], array[float], array[float], tuple[float, float, int, float]]:
    terr, psfc, ca, sa = shared_2d
    trim, k_exp, ny, nx = domain.bdy_trim_cells, domain.exported_levels_k, psfc.ny, psfc.nx
    shp3 = (k_exp, ny, nx)
    p_pa, p_buf = _validate_and_groom_3d_pressure(
        raw, _as_f64(psfc.buf, (ny, nx)), trim, k_exp, shp3
    )
    ph = _as_f64(crop_sponge_3d(destagger_z_3d(raw.ph_stag_m2s2), trim, k_exp).buf, shp3, "PH")
    phb = _as_f64(crop_sponge_3d(destagger_z_3d(raw.phb_stag_m2s2), trim, k_exp).buf, shp3, "PHB")
    z_buf = groom_f32_buf((ph + phb) / G0_MPS2, 17)
    th = _as_f64(crop_sponge_3d(raw.theta_pert_k, trim, k_exp).buf, shp3, "T")
    tk = (th + WRF_T0_K) * np.power(p_pa / P0_PA, KAPPA_DRY)
    qv_in = _as_f64(crop_sponge_3d(raw.qvapor_kgkg, trim, k_exp).buf, shp3, "QVAPOR")
    if bool(np.any(qv_in < -1e-4)):
        raise ValueError("Large negative QVAPOR encountered in post_frame")
    qv = np.maximum(0.0, qv_in)
    spfh = qv / (1.0 + qv)
    w_m = _as_f64(crop_sponge_3d(destagger_z_3d(raw.w_stag_mps), trim, k_exp).buf, shp3, "W")
    omega = -(p_pa / (R_DRY_J_KG_K * tk * (1.0 + VIRTUAL_TEMP_COEFF * spfh))) * G0_MPS2 * w_m
    u_g = _as_f64(crop_sponge_3d(destagger_x_3d(raw.u_stag_mps), trim, k_exp).buf, shp3, "U")
    v_g = _as_f64(crop_sponge_3d(destagger_y_3d(raw.v_stag_mps), trim, k_exp).buf, shp3, "V")
    ca_2d, sa_2d = _as_f64(ca.buf, (ny, nx), "COSALPHA"), _as_f64(sa.buf, (ny, nx), "SINALPHA")
    u_e, v_e = u_g * ca_2d - v_g * sa_2d, u_g * sa_2d + v_g * ca_2d
    agl = _as_f64(z_buf, shp3, "height_msl_m") - _as_f64(terr.buf, (ny, nx), "terrain_m")
    dz = np.concatenate([agl[:1], agl[1:] - agl[:-1]], axis=0)
    cfl = (
        np.abs(u_e) / domain.dx_m + np.abs(v_e) / domain.dy_m + np.abs(w_m) / np.maximum(10.0, dz)
    ) * float(science.time_step_s)
    bl_n = int(np.min(np.sum(agl <= BL_CEILING_AGL_M, axis=0)))
    stats = (float(np.min(dz)), float(np.max(dz)), bl_n, round(float(np.max(cfl)), 5))
    bufs = (
        ("pressure_pa", p_buf),
        ("height_msl_m", z_buf),
        ("temp_k", groom_f32_buf(tk, 15)),
        ("spfh_kgkg", groom_f32_buf(spfh, 15)),
        ("u_earth_mps", groom_f32_buf(u_e, 15)),
        ("v_earth_mps", groom_f32_buf(v_e, 15)),
        ("omega_pas", groom_f32_buf(omega, 15)),
    )
    f3d = {nm: PubMass3D(Slab3DF32(nz=k_exp, ny=ny, nx=nx, buf=b)) for nm, b in bufs}
    cld = crop_sponge_3d(raw.cldfra, trim, raw.cldfra.nz)
    cld_tot, cld_low = _compute_cloud_slabs(cld, p_buf, psfc, k_exp)
    return f3d, cld_tot, cld_low, stats


def _surface_accum_rates(
    prev: AccumBaseline, curr: AccumBaseline, sc: ScienceProfile, dt_s: float
) -> tuple[_ArrF64, _ArrF64, _ArrF64]:
    shp = (prev.achfx_j_m2.ny, prev.achfx_j_m2.nx)
    sh = (_as_f64(curr.achfx_j_m2.buf, shp, "achfx") - _as_f64(prev.achfx_j_m2.buf, shp)) / dt_s
    lh = (_as_f64(curr.aclhf_j_m2.buf, shp, "aclhf") - _as_f64(prev.aclhf_j_m2.buf, shp)) / dt_s

    def rain_rate(pv: PubMass2D, pi: PubBucket2D, cv: PubMass2D, ci: PubBucket2D) -> _ArrF64:
        p_i = np.frombuffer(pi.buf, dtype=np.int32).reshape(shp).astype(np.float64)
        c_i = np.frombuffer(ci.buf, dtype=np.int32).reshape(shp).astype(np.float64)
        d = (_as_f64(cv.buf, shp, "rain") + c_i * sc.bucket_mm) - (
            _as_f64(pv.buf, shp) + p_i * sc.bucket_mm
        )
        if bool(np.any(d < -1e-6)):
            raise ValueError(f"Negative cumulative precip delta ({float(np.min(d))}) across window")
        return np.maximum(0.0, d) * (3600.0 / dt_s)

    rc = rain_rate(prev.rainc_mm, prev.i_rainc, curr.rainc_mm, curr.i_rainc)
    return sh, lh, rc + rain_rate(prev.rainnc_mm, prev.i_rainnc, curr.rainnc_mm, curr.i_rainnc)


def _pack_2d_slabs(
    ny: int,
    nx: int,
    groomed_pairs: tuple[tuple[str, array[float]], ...],
    fixed_slabs: dict[str, PubMass2D],
) -> dict[str, PubMass2D]:
    out = {nm: PubMass2D(Slab2DF32(ny=ny, nx=nx, buf=b)) for nm, b in groomed_pairs}
    out.update(fixed_slabs)
    return out


def _post_surface_slabs(
    prev: AccumBaseline,
    curr: AccumBaseline,
    raw: RawWrfStepInput,
    trim: int,
    sc: ScienceProfile,
    dt_s: float,
    shared_2d: tuple[PubMass2D, PubMass2D, PubMass2D, PubMass2D],
    cld_tot: array[float],
    cld_low: array[float],
) -> dict[str, PubMass2D]:
    hgt, psfc, ca, sa = shared_2d
    ny, nx = hgt.ny, hgt.nx
    shp2 = (ny, nx)
    t2_s = _groom_2d(crop_sponge_2d(raw.t2_k, trim), 15, "T2")
    t2, psfc_f = _as_f64(t2_s.buf, shp2, "T2"), _as_f64(psfc.buf, shp2, "PSFC")
    q2_raw = np.maximum(0.0, _crop2_f64(raw.q2_kgkg, trim, "Q2"))
    sp2 = clamp_spfh_2m_vec(q2_raw / (1.0 + q2_raw), t2, psfc_f)
    sh, lh, pr = _surface_accum_rates(prev, curr, sc, dt_s)
    u10g = _crop2_f64(raw.u10_grid_mps, trim, "U10")
    v10g = _crop2_f64(raw.v10_grid_mps, trim, "V10")
    ca_f, sa_f = _as_f64(ca.buf, shp2, "COSALPHA"), _as_f64(sa.buf, shp2, "SINALPHA")
    ue, ve = u10g * ca_f - v10g * sa_f, u10g * sa_f + v10g * ca_f
    pblh_s = crop_sponge_2d(raw.pblh_m, trim)
    hgt_f, pbl_f = _as_f64(hgt.buf, shp2, "HGT"), _as_f64(pblh_s.buf, shp2, "PBLH")
    afwa_m = _crop2_f64(raw.afwa_mslp_pa, trim, "AFWA_MSLP") if raw.afwa_mslp_pa else None
    use_afwa = afwa_m is not None and bool(np.all(afwa_m > 0.0))
    mslp = (
        afwa_m if use_afwa and afwa_m is not None else hypsometric_mslp_vec(psfc_f, hgt_f, t2, sp2)
    )
    wmax, ust = (
        _crop2_f64(raw.wspd10max_mps, trim, "WSPD10MAX"),
        _crop2_f64(raw.ustar_mps, trim, "UST"),
    )
    gust = parameterized_gust_10m_vec(ue, ve, wmax, ust, sh, lh, pbl_f, t2, sp2, psfc_f)
    pairs = (
        ("spfh_2m_kgkg", groom_f32_buf(sp2, 15)),
        ("sensible_heat_flux_wm2", groom_f32_buf(sh, 15)),
        ("latent_heat_flux_wm2", groom_f32_buf(lh, 15)),
        ("precip_mm_hr", groom_f32_buf(pr, 15)),
        ("mslp_pa", groom_f32_buf(mslp, 17)),
        ("u_10m_earth_mps", groom_f32_buf(ue, 15)),
        ("v_10m_earth_mps", groom_f32_buf(ve, 15)),
        ("gust_10m_mps", groom_f32_buf(gust, 15)),
        ("cloud_total_pct", cld_tot),
        ("cloud_low_pct", cld_low),
    )
    fixed = {
        "terrain_m": hgt,
        "surface_pressure_pa": psfc,
        "temp_2m_k": t2_s,
        "pbl_height_m": _groom_2d(pblh_s, 15, "PBLH"),
        "cape_jkg": _groom_2d(crop_sponge_2d(raw.afwa_cape_jkg, trim), 15, "AFWA_CAPE"),
    }
    return _pack_2d_slabs(ny, nx, pairs, fixed)


def _ensure_raw_step_finite(raw: RawWrfStepInput) -> None:
    for sl in (
        raw.p_pert_pa.buf,
        raw.pb_pa.buf,
        raw.ph_stag_m2s2.buf,
        raw.phb_stag_m2s2.buf,
        raw.theta_pert_k.buf,
        raw.qvapor_kgkg.buf,
        raw.cldfra.buf,
        raw.u_stag_mps.buf,
        raw.v_stag_mps.buf,
        raw.w_stag_mps.buf,
    ):
        if not bool(np.all(np.isfinite(np.frombuffer(sl, dtype=np.float32)))):
            raise ValueError("Non-finite float in computational-grid WRF step input")


def post_frame(
    prev: AccumBaseline, raw: RawWrfStepInput, domain: DomainProfile, science: ScienceProfile
) -> tuple[NormalizedWrfFrame, AccumBaseline]:
    """Transform one raw WRF step + predecessor baseline into all 22 Published-Grid arrays."""
    dt_s = float(raw.valid_time_unix_s - prev.valid_time_unix_s)
    if dt_s <= 0.0:
        raise ValueError("raw.valid_time_unix_s must strictly exceed prev.valid_time_unix_s")
    _ensure_raw_step_finite(raw)
    trim = domain.bdy_trim_cells
    shared_2d = (
        _groom_2d(crop_sponge_2d(raw.terrain_m, trim), 17, "HGT"),
        _groom_2d(crop_sponge_2d(raw.psfc_pa, trim), 17, "PSFC"),
        crop_sponge_2d(raw.cos_alpha, trim),
        crop_sponge_2d(raw.sin_alpha, trim),
    )
    if (shared_2d[0].ny, shared_2d[0].nx) != published_grid_shape(domain):
        raise ValueError("Cropped Published Grid shape mismatch")
    f3d, cld_tot, cld_low, (min_dz, max_dz, min_bl, max_cfl) = _post_3d_slabs(
        raw, domain, science, shared_2d
    )
    next_base = build_accum_baseline(raw, trim)
    f2d = _post_surface_slabs(
        prev, next_base, raw, trim, science, dt_s, shared_2d, cld_tot, cld_low
    )
    frame = NormalizedWrfFrame(
        raw.valid_time_utc,
        prev.valid_time_unix_s,
        raw.valid_time_unix_s,
        f2d,
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
