"""Vectorized stateful WRF -> Published-Grid Zarr slab post-processing (`post_frame`).

Assumptions & Field Semantics:
- Trailing statistical windows: `sensible_heat_flux_wm2`, `latent_heat_flux_wm2`, and
  `precip_mm_hr` represent trailing-window means across `(t_prev, t_curr]` (`1 h` in hourly
  segments, `3 h` in 3-hourly tail segments). Surface heat fluxes allow signed negative nocturnal
  values; rainfall requires non-negative cumulative deltas.
- Low-cloud threshold: `p >= 0.8 * PSFC` follows ECMWF terrain-following sigma (`LCC`) so high
  plateaus (`PSFC < 800 hPa`) retain true low-cloud / fog detection.
- `height_msl_m`: `(PH + PHB) / 9.81` destaggered at exact `eta` midpoints (`0.5*(face_k+face_k+1)`
  per Skamarock et al. 2019 Sec. 3.2.1), matching `terrain_m` (`HGT`) datum at ground.
"""

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
    CONSUMER_EPSILON,
    CONSUMER_KELVIN_OFFSET,
    CP_DRY_J_KG_K,
    G0_MPS2,
    IFS_GUST_COEFF,
    KAPPA_DRY,
    LV_VAPOR_J_KG,
    P0_PA,
    R_DRY_J_KG_K,
    STD_LAPSE_K_PER_M,
    VIRTUAL_TEMP_COEFF,
    VON_KARMAN,
    groom_f32_buf,
)

type _ArrF64 = npt.NDArray[np.float64]


def _as_f64(buf: array[float], shape: tuple[int, ...]) -> _ArrF64:
    return np.frombuffer(buf, dtype=np.float32).reshape(shape).astype(np.float64)


def _crop2_f64(slab: CompMass2D, trim: int) -> _ArrF64:
    cropped = crop_sponge_2d(slab, trim)
    return _as_f64(cropped.buf, (cropped.ny, cropped.nx))


def _groom_2d(slab: PubMass2D, keepbits: int) -> PubMass2D:
    arr = _as_f64(slab.buf, (slab.ny, slab.nx))
    return PubMass2D(Slab2DF32(ny=slab.ny, nx=slab.nx, buf=groom_f32_buf(arr, keepbits)))


def build_accum_baseline(raw: RawWrfStepInput, trim: int) -> AccumBaseline:
    """Crop both float32 accumulators and int32 bucket counters onto Published Grid."""
    return AccumBaseline(
        valid_time_utc=raw.valid_time_utc,
        valid_time_unix_s=raw.valid_time_unix_s,
        achfx_j_m2=crop_sponge_2d(raw.achfx_j_m2, trim),
        i_achfx=crop_bucket_2d(raw.i_achfx, trim),
        aclhf_j_m2=crop_sponge_2d(raw.aclhf_j_m2, trim),
        i_aclhf=crop_bucket_2d(raw.i_aclhf, trim),
        rainc_mm=crop_sponge_2d(raw.rainc_mm, trim),
        i_rainc=crop_bucket_2d(raw.i_rainc, trim),
        rainnc_mm=crop_sponge_2d(raw.rainnc_mm, trim),
        i_rainnc=crop_bucket_2d(raw.i_rainnc, trim),
    )


def _compute_cloud_slabs(
    cld_all: PubMass3D, p_groomed: array[float], psfc: PubMass2D, k_exp: int
) -> tuple[array[float], array[float]]:
    plane = psfc.ny * psfc.nx
    c = np.clip(_as_f64(cld_all.buf, (cld_all.nz, plane)), 0.0, 1.0)
    clear = 1.0 - c[0]
    for k in range(1, c.shape[0]):
        prev, curr = c[k - 1], c[k]
        denom = np.where(prev < 0.999999, 1.0 - prev, 1.0)
        ratio = np.clip((1.0 - np.maximum(prev, curr)) / denom, 0.0, 1.0)
        clear = np.where(prev < 0.999999, clear * ratio, 0.0)
    tot_pct = 100.0 * (1.0 - np.clip(clear, 0.0, 1.0))

    p_2d, psfc_1d = _as_f64(p_groomed, (k_exp, plane)), _as_f64(psfc.buf, (plane,))
    clear_low, prev_low = np.ones(plane, dtype=np.float64), np.zeros(plane, dtype=np.float64)
    started = np.zeros(plane, dtype=bool)
    for k in range(k_exp):
        inc, ck = p_2d[k] >= 0.8 * psfc_1d, c[k]
        denom = np.where(prev_low < 0.999999, 1.0 - prev_low, 1.0)
        ratio = np.clip((1.0 - np.maximum(prev_low, ck)) / denom, 0.0, 1.0)
        step_clear = np.where(prev_low < 0.999999, clear_low * ratio, 0.0)
        clear_low = np.where(inc, np.where(started, step_clear, 1.0 - ck), clear_low)
        prev_low, started = np.where(inc, ck, prev_low), started | inc
    low_pct = 100.0 * (1.0 - np.clip(clear_low, 0.0, 1.0))
    return groom_f32_buf(tot_pct, 15), groom_f32_buf(low_pct, 15)


def _post_3d_slabs(
    raw: RawWrfStepInput,
    domain: DomainProfile,
    science: ScienceProfile,
    shared_2d: tuple[PubMass2D, PubMass2D, PubMass2D, PubMass2D],
) -> tuple[dict[str, PubMass3D], array[float], array[float], tuple[float, float, int, float]]:
    terr, psfc, ca, sa = shared_2d
    trim, k_exp, ny, nx = domain.bdy_trim_cells, domain.exported_levels_k, psfc.ny, psfc.nx
    shp3 = (k_exp, ny, nx)
    p_pa = _as_f64(crop_sponge_3d(raw.p_pert_pa, trim, k_exp).buf, shp3) + _as_f64(
        crop_sponge_3d(raw.pb_pa, trim, k_exp).buf, shp3
    )
    if bool(np.any(p_pa <= 0.0)):
        raise ValueError("Non-positive 3D pressure encountered in post_frame")
    ph_m = _as_f64(crop_sponge_3d(destagger_z_3d(raw.ph_stag_m2s2), trim, k_exp).buf, shp3)
    phb_m = _as_f64(crop_sponge_3d(destagger_z_3d(raw.phb_stag_m2s2), trim, k_exp).buf, shp3)
    z_msl = (ph_m + phb_m) / G0_MPS2
    th_p = _as_f64(crop_sponge_3d(raw.theta_pert_k, trim, k_exp).buf, shp3)
    tk = (th_p + science.base_temp_k) * np.power(p_pa / P0_PA, KAPPA_DRY)
    qv = np.maximum(0.0, _as_f64(crop_sponge_3d(raw.qvapor_kgkg, trim, k_exp).buf, shp3))
    spfh = qv / (1.0 + qv)
    w_m = _as_f64(crop_sponge_3d(destagger_z_3d(raw.w_stag_mps), trim, k_exp).buf, shp3)
    omega = -(p_pa / (R_DRY_J_KG_K * tk * (1.0 + VIRTUAL_TEMP_COEFF * spfh))) * G0_MPS2 * w_m
    u_g = _as_f64(crop_sponge_3d(destagger_x_3d(raw.u_stag_mps), trim, k_exp).buf, shp3)
    v_g = _as_f64(crop_sponge_3d(destagger_y_3d(raw.v_stag_mps), trim, k_exp).buf, shp3)
    ca_2d, sa_2d = _as_f64(ca.buf, (ny, nx)), _as_f64(sa.buf, (ny, nx))
    u_e, v_e = u_g * ca_2d - v_g * sa_2d, u_g * sa_2d + v_g * ca_2d

    p_buf, z_buf = groom_f32_buf(p_pa, 17), groom_f32_buf(z_msl, 17)
    agl = _as_f64(z_buf, shp3) - _as_f64(terr.buf, (ny, nx))
    dz = np.empty_like(agl)
    dz[0], dz[1:] = agl[0], agl[1:] - agl[:-1]
    cfl = (
        np.abs(u_e) / domain.dx_m + np.abs(v_e) / domain.dy_m + np.abs(w_m) / np.maximum(10.0, dz)
    ) * float(science.time_step_s)
    stats = (
        float(np.min(dz)),
        float(np.max(dz)),
        int(np.min(np.sum(agl <= BL_CEILING_AGL_M, axis=0))),
        round(float(np.max(cfl)), 5),
    )

    def wrap3(b: array[float]) -> PubMass3D:
        return PubMass3D(Slab3DF32(nz=k_exp, ny=ny, nx=nx, buf=b))

    f3d = {
        "pressure_pa": wrap3(p_buf),
        "height_msl_m": wrap3(z_buf),
        "temp_k": wrap3(groom_f32_buf(tk, 15)),
        "spfh_kgkg": wrap3(groom_f32_buf(spfh, 15)),
        "u_earth_mps": wrap3(groom_f32_buf(u_e, 15)),
        "v_earth_mps": wrap3(groom_f32_buf(v_e, 15)),
        "omega_pas": wrap3(groom_f32_buf(omega, 15)),
    }
    cld_tot, cld_low = _compute_cloud_slabs(
        crop_sponge_3d(raw.cldfra, trim, raw.cldfra.nz), p_buf, psfc, k_exp
    )
    return f3d, cld_tot, cld_low, stats


def _surface_accum_rates(
    prev: AccumBaseline, curr: AccumBaseline, sc: ScienceProfile, dt_s: float
) -> tuple[_ArrF64, _ArrF64, _ArrF64]:
    def rate(
        pv: PubMass2D, pi: PubBucket2D, cv: PubMass2D, ci: PubBucket2D, s: float, neg: bool
    ) -> _ArrF64:
        shp = (pv.ny, pv.nx)
        p_i = np.frombuffer(pi.buf, dtype=np.int32).reshape(shp).astype(np.float64)
        c_i = np.frombuffer(ci.buf, dtype=np.int32).reshape(shp).astype(np.float64)
        d = (_as_f64(cv.buf, shp) + c_i * s) - (_as_f64(pv.buf, shp) + p_i * s)
        if not neg and bool(np.any(d < -1e-6)):
            raise ValueError(f"Negative cumulative delta ({float(np.min(d))}) across window")
        return (d / dt_s) if neg else (np.maximum(0.0, d) * (3600.0 / dt_s))

    sh = rate(prev.achfx_j_m2, prev.i_achfx, curr.achfx_j_m2, curr.i_achfx, sc.bucket_j, True)
    lh = rate(prev.aclhf_j_m2, prev.i_aclhf, curr.aclhf_j_m2, curr.i_aclhf, sc.bucket_j, True)
    rc = rate(prev.rainc_mm, prev.i_rainc, curr.rainc_mm, curr.i_rainc, sc.bucket_mm, False)
    rn = rate(prev.rainnc_mm, prev.i_rainnc, curr.rainnc_mm, curr.i_rainnc, sc.bucket_mm, False)
    return sh, lh, rc + rn


def _derive_mslp_and_gust(
    sfc: tuple[_ArrF64, _ArrF64, _ArrF64, _ArrF64],
    wind: tuple[_ArrF64, _ArrF64, _ArrF64, _ArrF64],
    flux_pbl: tuple[_ArrF64, _ArrF64, _ArrF64],
) -> tuple[_ArrF64, _ArrF64]:
    psfc, hgt, t2, sp2 = sfc
    ue, ve, wmax, ust = wind
    sh, lh, pblh = flux_pbl
    z_m = np.maximum(0.0, hgt)
    tv_sfc = t2 * (1.0 + VIRTUAL_TEMP_COEFF * np.maximum(0.0, sp2))
    tv_0 = np.where(tv_sfc < 255.0, 0.5 * (255.0 + tv_sfc), tv_sfc)
    tv_sl = tv_0 + STD_LAPSE_K_PER_M * z_m
    warm_cap, hot_plat = 290.5 - 0.005 * np.square(tv_0 - 290.5), 0.5 * (290.5 + tv_0)
    tv_sl_out = np.where(
        (tv_0 <= 290.5) & (tv_sl > 290.5),
        warm_cap,
        np.where((tv_0 > 290.5) & (tv_sl > 290.5), hot_plat, tv_sl),
    )
    tv_0_out = np.where((tv_0 > 290.5) & (tv_sl > 290.5), hot_plat, tv_0)
    tv_m = np.maximum(180.0, 0.5 * (tv_0_out + tv_sl_out))
    mslp = psfc * np.exp((G0_MPS2 * z_m) / (R_DRY_J_KG_K * tv_m))

    q2, tk = np.maximum(0.0, sp2), np.maximum(200.0, t2)
    tv0 = tk * (1.0 + VIRTUAL_TEMP_COEFF * q2)
    rho = np.maximum(10_000.0, psfc) / (R_DRY_J_KG_K * tv0)
    vflux = (1.0 + VIRTUAL_TEMP_COEFF * q2) * (sh / (rho * CP_DRY_J_KG_K)) + (
        VIRTUAL_TEMP_COEFF * tk * (lh / (rho * LV_VAPOR_J_KG))
    )
    wstar3 = (G0_MPS2 * np.maximum(0.0, pblh) / tv0) * np.maximum(0.0, vflux)
    eddy = IFS_GUST_COEFF * np.cbrt(
        np.power(np.maximum(0.0, ust), 3.0) + (0.5 * VON_KARMAN / 12.0) * wstar3
    )
    return mslp, np.maximum(wmax, np.hypot(ue, ve) + eddy)


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
    t2_s = _groom_2d(crop_sponge_2d(raw.t2_k, trim), 15)
    t2, psfc_f, hgt_f = _as_f64(t2_s.buf, shp2), _as_f64(psfc.buf, shp2), _as_f64(hgt.buf, shp2)
    q2_raw = np.maximum(0.0, _crop2_f64(raw.q2_kgkg, trim))
    sp2_raw = q2_raw / (1.0 + q2_raw)
    es_pa = 611.2 * np.exp((17.67 * (t2 - CONSUMER_KELVIN_OFFSET)) / np.maximum(1.0, t2 - 29.65))
    safe_p = psfc_f > es_pa * 1.01
    r_sat = np.where(safe_p, CONSUMER_EPSILON * es_pa / np.maximum(1.0, psfc_f - es_pa), sp2_raw)
    sp2 = np.where(safe_p, np.minimum(sp2_raw, (r_sat / (1.0 + r_sat)) * (1.0 - 2.0**-14)), sp2_raw)

    sh, lh, pr = _surface_accum_rates(prev, curr, sc, dt_s)
    u10g, v10g = _crop2_f64(raw.u10_grid_mps, trim), _crop2_f64(raw.v10_grid_mps, trim)
    ca_f, sa_f = _as_f64(ca.buf, shp2), _as_f64(sa.buf, shp2)
    ue, ve = u10g * ca_f - v10g * sa_f, u10g * sa_f + v10g * ca_f
    pblh_s = crop_sponge_2d(raw.pblh_m, trim)
    mslp, gust = _derive_mslp_and_gust(
        (psfc_f, hgt_f, t2, sp2),
        (ue, ve, _crop2_f64(raw.wspd10max_mps, trim), _crop2_f64(raw.ustar_mps, trim)),
        (sh, lh, _as_f64(pblh_s.buf, shp2)),
    )

    def wrap2(b: array[float]) -> PubMass2D:
        return PubMass2D(Slab2DF32(ny=ny, nx=nx, buf=b))

    return {
        "terrain_m": hgt,
        "surface_pressure_pa": psfc,
        "temp_2m_k": t2_s,
        "spfh_2m_kgkg": wrap2(groom_f32_buf(sp2, 15)),
        "sensible_heat_flux_wm2": wrap2(groom_f32_buf(sh, 15)),
        "latent_heat_flux_wm2": wrap2(groom_f32_buf(lh, 15)),
        "precip_mm_hr": wrap2(groom_f32_buf(pr, 15)),
        "mslp_pa": wrap2(groom_f32_buf(mslp, 17)),
        "u_10m_earth_mps": wrap2(groom_f32_buf(ue, 15)),
        "v_10m_earth_mps": wrap2(groom_f32_buf(ve, 15)),
        "gust_10m_mps": wrap2(groom_f32_buf(gust, 15)),
        "pbl_height_m": _groom_2d(pblh_s, 15),
        "cape_jkg": _groom_2d(crop_sponge_2d(raw.afwa_cape_jkg, trim), 15),
        "cloud_total_pct": wrap2(cld_tot),
        "cloud_low_pct": wrap2(cld_low),
    }


def post_frame(
    prev: AccumBaseline, raw: RawWrfStepInput, domain: DomainProfile, science: ScienceProfile
) -> tuple[NormalizedWrfFrame, AccumBaseline]:
    """Transform one raw WRF step + predecessor baseline into all 22 Published-Grid arrays."""
    dt_s = float(raw.valid_time_unix_s - prev.valid_time_unix_s)
    if dt_s <= 0.0:
        raise ValueError("raw.valid_time_unix_s must strictly exceed prev.valid_time_unix_s")
    nj, ni = published_grid_shape(domain)
    trim = domain.bdy_trim_cells
    shared_2d = (
        _groom_2d(crop_sponge_2d(raw.terrain_m, trim), 17),
        _groom_2d(crop_sponge_2d(raw.psfc_pa, trim), 17),
        crop_sponge_2d(raw.cos_alpha, trim),
        crop_sponge_2d(raw.sin_alpha, trim),
    )
    if (shared_2d[0].ny, shared_2d[0].nx) != (nj, ni):
        raise ValueError("Cropped Published Grid shape mismatch")
    fields_3d, cld_tot, cld_low, (min_dz, max_dz, min_bl, max_cfl) = _post_3d_slabs(
        raw, domain, science, shared_2d
    )
    next_base = build_accum_baseline(raw, trim)
    fields_2d = _post_surface_slabs(
        prev, next_base, raw, trim, science, dt_s, shared_2d, cld_tot, cld_low
    )
    return (
        NormalizedWrfFrame(
            valid_time_utc=raw.valid_time_utc,
            window_start_unix_s=prev.valid_time_unix_s,
            window_end_unix_s=raw.valid_time_unix_s,
            fields_2d=fields_2d,
            fields_3d=fields_3d,
            min_dz_m=min_dz,
            max_dz_m=max_dz,
            min_bl_levels_2km=min_bl,
            max_cfl=max_cfl,
        ),
        next_base,
    )


__all__ = ["NormalizedWrfFrame", "RawWrfStepInput", "build_accum_baseline", "post_frame"]
