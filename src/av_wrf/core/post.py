"""Vectorized stateful WRF -> Published-Grid Zarr slab post-processing (`post_frame`)."""

from __future__ import annotations

from array import array

import numpy as np
import numpy.typing as npt

from av_wrf.contracts import (
    AccumBaseline,
    CompMass2D,
    CompMass3D,
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
    _crop_sponge_2d_view,
    crop_bucket_2d,
    crop_destagger_x_3d,
    crop_destagger_y_3d,
    crop_destagger_z_3d,
    crop_sponge_2d,
    published_grid_shape,
)
from av_wrf.core.thermo import (
    G0_MPS2,
    afwa_mslp_vec,
    clamp_spfh_2m_vec,
    geometric_w_to_omega_vec,
    groom_f32_buf,
    max_random_cloud_cover_pct,
    mixing_ratio_to_spfh_vec,
    parameterized_gust_10m_vec,
    potential_to_temp_vec,
    rotate_grid_to_earth_vec,
    validate_thermodynamic_field,
)
from av_wrf.core.vertical import model_column_spacing

type _ArrF64 = npt.NDArray[np.float64]
type _SharedSurface = tuple[_ArrF64, _ArrF64, _ArrF64, _ArrF64]


def _as_f64(buf: array[float], shape: tuple[int, ...], label: str | None = None) -> _ArrF64:
    arr = np.frombuffer(buf, dtype=np.float32).reshape(shape)
    if label is not None and not bool(np.all(np.isfinite(arr))):
        raise ValueError(f"Non-finite value encountered in {label}")
    return arr.astype(np.float64)


def _mass_view(slab: CompMass3D, trim: int, shape: tuple[int, int, int]) -> npt.NDArray[np.float32]:
    nz, ny, nx = slab.nz, slab.ny, slab.nx
    sub = np.frombuffer(slab.buf, dtype=np.float32).reshape(nz, ny, nx)
    sub = sub[: shape[0], trim:-trim, trim:-trim]
    if sub.shape != shape:
        raise ValueError(f"Cropped mass volume shape {sub.shape} != {shape}")
    return sub


def _crop2_f64(slab: CompMass2D, trim: int, label: str, *, nonnegative: bool = False) -> _ArrF64:
    view = _crop_sponge_2d_view(slab, trim)
    if not bool(np.all(np.isfinite(view))):
        raise ValueError(f"Non-finite value encountered in {label}")
    if nonnegative and np.any(view < 0.0):
        raise ValueError(f"Negative value encountered in {label}")
    return view.astype(np.float64)


def _groom_2d(values: _ArrF64, keepbits: int) -> PubMass2D:
    ny, nx = values.shape
    return PubMass2D(Slab2DF32(ny, nx, groom_f32_buf(values, keepbits)))


def build_accum_baseline(raw: RawWrfBaselineInput | RawWrfStepInput, trim: int) -> AccumBaseline:
    """Crop signed heat-flux accumulators and rainfall buckets onto Published Grid."""
    if bool(np.any(_crop2_f64(raw.psfc_pa, trim, "baseline.psfc_pa") <= 0.0)):
        raise ValueError("Non-positive surface pressure in accumulator baseline")
    return _crop_accum_baseline(raw, trim)


def _crop_accum_baseline(raw: RawWrfBaselineInput | RawWrfStepInput, trim: int) -> AccumBaseline:
    achfx, aclhf = crop_sponge_2d(raw.achfx_j_m2, trim), crop_sponge_2d(raw.aclhf_j_m2, trim)
    rainc, rainnc = crop_sponge_2d(raw.rainc_mm, trim), crop_sponge_2d(raw.rainnc_mm, trim)
    i_rc, i_rn = crop_bucket_2d(raw.i_rainc, trim), crop_bucket_2d(raw.i_rainnc, trim)
    return AccumBaseline(
        raw.valid_time_utc, raw.valid_time_unix_s, achfx, aclhf, rainc, i_rc, rainnc, i_rn
    )


def _validate_and_groom_3d_pressure(
    raw: RawWrfStepInput, psfc_2d: _ArrF64, trim: int, k_exp: int
) -> tuple[_ArrF64, array[float]]:
    ny, nx = psfc_2d.shape
    shape = (raw.cldfra.nz, ny, nx)
    p_pa = np.add(
        _mass_view(raw.p_pert_pa, trim, shape),
        _mass_view(raw.pb_pa, trim, shape),
        dtype=np.float64,
    )
    if not np.all(p_pa[-1] > 0.0):
        raise ValueError("Non-positive 3D pressure in post_frame")
    if not np.all(p_pa[1:] < p_pa[:-1]):
        raise ValueError("Non-monotonic 3D column pressure (pressure must decrease with height)")
    p_buf = groom_f32_buf(p_pa[:k_exp], 17)
    p_gr = np.frombuffer(p_buf, dtype=np.float32).reshape(k_exp, ny, nx)
    if bool(np.any(p_gr[0] >= psfc_2d)) or bool(np.any(p_gr[1:] >= p_gr[:-1])):
        raise ValueError("Bit-groomed 3D column pressure violated monotonic ordering")
    return p_pa, p_buf


def _post_mslp(
    raw: RawWrfStepInput, trim: int, first_height: _ArrF64, first_temp: _ArrF64, first_qv: _ArrF64
) -> PubMass2D:
    mslp = (
        np.zeros_like(first_height)
        if raw.afwa_mslp_pa is None
        else _crop2_f64(raw.afwa_mslp_pa, trim, "AFWA_MSLP")
    )
    missing = mslp <= 0.0
    if np.any(missing):
        mslp[missing] = afwa_mslp_vec(
            _crop_sponge_2d_view(raw.psfc_pa, trim)[missing].astype(np.float64),
            _crop_sponge_2d_view(raw.terrain_m, trim)[missing].astype(np.float64),
            first_height[missing],
            first_temp[missing],
            first_qv[missing],
        )
    return _groom_2d(mslp, 17)


def _post_3d_slabs(
    raw: RawWrfStepInput,
    domain: DomainProfile,
    science: ScienceProfile,
    shared_2d: _SharedSurface,
) -> tuple[dict[str, PubMass3D], dict[str, PubMass2D], tuple[float, float, int, float]]:
    terr, psfc, ca, sa = shared_2d
    trim, k_exp = domain.bdy_trim_cells, domain.exported_levels_k
    ny, nx = psfc.shape
    shp3 = (k_exp, ny, nx)
    p_all, p_buf = _validate_and_groom_3d_pressure(raw, psfc, trim, k_exp)
    cloud = _mass_view(raw.cldfra, trim, (raw.cldfra.nz, ny, nx))
    cld_tot, cld_low = max_random_cloud_cover_pct(
        cloud, p_all, _crop_sponge_2d_view(raw.psfc_pa, trim)
    )
    p_pa = p_all[:k_exp]
    ph = np.frombuffer(crop_destagger_z_3d(raw.ph_stag_m2s2, trim, k_exp).buf, np.float32)
    phb = np.frombuffer(crop_destagger_z_3d(raw.phb_stag_m2s2, trim, k_exp).buf, np.float32)
    height_m = np.add(ph, phb, dtype=np.float64).reshape(shp3)
    height_m /= G0_MPS2
    z_buf = groom_f32_buf(height_m, 17)
    th = _mass_view(raw.theta_pert_k, trim, shp3).astype(np.float64)
    tk = potential_to_temp_vec(th, p_pa)
    t_buf = groom_f32_buf(tk, 15)
    validate_thermodynamic_field("temp_k", np.frombuffer(t_buf, np.float32))
    qv_in = _mass_view(raw.qvapor_kgkg, trim, shp3).astype(np.float64)
    if bool(np.any(qv_in < -1e-4)):
        raise ValueError("Large negative QVAPOR encountered in post_frame")
    column_fields = {
        "cloud_total_pct": _groom_2d(cld_tot, 15),
        "cloud_low_pct": _groom_2d(cld_low, 15),
        "mslp_pa": _post_mslp(raw, trim, height_m[0], tk[0], qv_in[0]),
    }
    spfh = mixing_ratio_to_spfh_vec(qv_in)
    if bool(np.any(spfh >= 1.0)):
        raise ValueError("Invalid specific humidity in 3D column")
    # Averages of the previously validated faces remain finite through float32 rounding.
    w_m = _as_f64(crop_destagger_z_3d(raw.w_stag_mps, trim, k_exp).buf, shp3)
    omega = geometric_w_to_omega_vec(w_m, p_pa, tk, spfh)
    u_g = _as_f64(crop_destagger_x_3d(raw.u_stag_mps, trim, k_exp).buf, shp3)
    v_g = _as_f64(crop_destagger_y_3d(raw.v_stag_mps, trim, k_exp).buf, shp3)
    u_e, v_e = rotate_grid_to_earth_vec(u_g, v_g, ca, sa)
    agl = np.subtract(np.frombuffer(z_buf, np.float32).reshape(shp3), terr, out=height_m)
    dz, bl_n = model_column_spacing(agl)
    cfl = (
        np.abs(u_g) / domain.dx_m + np.abs(v_g) / domain.dy_m + np.abs(w_m) / np.maximum(10.0, dz)
    ) * float(science.time_step_s)
    stats = (float(np.min(dz)), float(np.max(dz)), bl_n, round(float(np.max(cfl)), 5))
    bufs = (
        ("pressure_pa", p_buf),
        ("height_msl_m", z_buf),
        ("temp_k", t_buf),
        ("spfh_kgkg", groom_f32_buf(spfh, 15)),
        ("u_earth_mps", groom_f32_buf(u_e, 15)),
        ("v_earth_mps", groom_f32_buf(v_e, 15)),
        ("omega_pas", groom_f32_buf(omega, 15)),
    )
    f3d = {nm: PubMass3D(Slab3DF32(nz=k_exp, ny=ny, nx=nx, buf=b)) for nm, b in bufs}
    return f3d, column_fields, stats


def _surface_accum_rates(
    prev: AccumBaseline, curr: AccumBaseline, bucket_mm: float
) -> tuple[_ArrF64, _ArrF64, _ArrF64]:
    dt_s = float(curr.valid_time_unix_s - prev.valid_time_unix_s)
    shp = (prev.achfx_j_m2.ny, prev.achfx_j_m2.nx)
    sh = (_as_f64(curr.achfx_j_m2.buf, shp, "achfx") - _as_f64(prev.achfx_j_m2.buf, shp)) / dt_s
    lh = (_as_f64(curr.aclhf_j_m2.buf, shp, "aclhf") - _as_f64(prev.aclhf_j_m2.buf, shp)) / dt_s

    def rain_rate(pv: PubMass2D, pi: PubBucket2D, cv: PubMass2D, ci: PubBucket2D) -> _ArrF64:
        p_i = np.frombuffer(pi.buf, dtype=np.int32).reshape(shp)
        c_i = np.frombuffer(ci.buf, dtype=np.int32).reshape(shp)
        if bool(np.any(p_i < 0)) or bool(np.any(c_i < p_i)):
            raise ValueError("Invalid or decreasing cumulative precipitation bucket count")
        d = _as_f64(cv.buf, shp, "rain") - _as_f64(pv.buf, shp, "rain")
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
    shared_2d: _SharedSurface,
) -> dict[str, PubMass2D]:
    _, psfc, ca, sa = shared_2d
    ny, nx = psfc.shape
    t2_s = _groom_2d(_crop2_f64(raw.t2_k, trim, "T2"), 15)
    t2_gr = np.frombuffer(t2_s.buf, np.float32).reshape(ny, nx)
    validate_thermodynamic_field("temp_2m_k", t2_gr)
    t2 = t2_gr.astype(np.float64)
    q2 = mixing_ratio_to_spfh_vec(_crop2_f64(raw.q2_kgkg, trim, "Q2"))
    sp2 = clamp_spfh_2m_vec(q2, t2, psfc)
    sh, lh, pr = _surface_accum_rates(prev, curr, sc.bucket_mm)
    u10g = _crop2_f64(raw.u10_grid_mps, trim, "U10")
    v10g = _crop2_f64(raw.v10_grid_mps, trim, "V10")
    ue, ve = rotate_grid_to_earth_vec(u10g, v10g, ca, sa)
    pblh = _crop2_f64(raw.pblh_m, trim, "PBLH", nonnegative=True)
    cape = _crop2_f64(raw.afwa_cape_jkg, trim, "AFWA_CAPE_MU", nonnegative=True)
    gust = parameterized_gust_10m_vec(
        ue,
        ve,
        _crop2_f64(raw.ustar_mps, trim, "UST"),
        _crop2_f64(raw.hfx_wm2, trim, "HFX"),
        _crop2_f64(raw.lh_wm2, trim, "LH"),
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
        "pbl_height_m": _groom_2d(pblh, 15),
        "cape_jkg": _groom_2d(cape, 15),
        **{nm: PubMass2D(Slab2DF32(ny=ny, nx=nx, buf=b)) for nm, b in pairs},
    }


def post_frame(
    prev: AccumBaseline, raw: RawWrfStepInput, domain: DomainProfile, science: ScienceProfile
) -> tuple[NormalizedWrfFrame, AccumBaseline]:
    """Transform one validated finite WRF step and baseline into 22 Published-Grid arrays."""
    if raw.valid_time_unix_s <= prev.valid_time_unix_s:
        raise ValueError("raw.valid_time_unix_s must strictly exceed prev.valid_time_unix_s")
    trim = domain.bdy_trim_cells
    hgt = _groom_2d(_crop2_f64(raw.terrain_m, trim, "HGT"), 17)
    psfc = _groom_2d(_crop2_f64(raw.psfc_pa, trim, "PSFC"), 17)
    validate_thermodynamic_field("surface_pressure_pa", np.frombuffer(psfc.buf, np.float32))
    shape = (hgt.ny, hgt.nx)
    if shape != published_grid_shape(domain):
        raise ValueError("Cropped Published Grid shape mismatch")
    shared_2d = (
        _as_f64(hgt.buf, shape, "HGT"),
        _as_f64(psfc.buf, shape, "PSFC"),
        _crop2_f64(raw.cos_alpha, trim, "COSALPHA"),
        _crop2_f64(raw.sin_alpha, trim, "SINALPHA"),
    )
    f3d, column_fields, (min_dz, max_dz, min_bl, max_cfl) = _post_3d_slabs(
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
