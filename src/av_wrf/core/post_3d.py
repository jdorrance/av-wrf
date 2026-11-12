"""3D thermodynamics, cloud and momentum slab construction for the Published Grid."""

from __future__ import annotations

from array import array

import numpy as np
import numpy.typing as npt

from av_wrf.contracts import (
    CompMass2D,
    CompMass3D,
    DomainProfile,
    PubMass2D,
    PubMass3D,
    RawWrfStepInput,
    ScienceProfile,
    Slab2DF32,
    Slab3DF32,
)
from av_wrf.core.grid import (
    _crop_sponge_2d_view,
    crop_destagger_x_3d,
    crop_destagger_y_3d,
    crop_destagger_z_3d,
    lambert_map_scale,
)
from av_wrf.core.thermo import (
    G0_MPS2,
    afwa_mslp_vec,
    geometric_w_to_omega_vec,
    groom_f32_buf,
    max_random_cloud_cover_pct,
    mixing_ratio_to_spfh_vec,
    potential_to_temp_vec,
    rotate_grid_to_earth_vec,
    validate_thermodynamic_field,
)
from av_wrf.core.vertical import model_column_spacing, staggered_face_phi

type ArrF64 = npt.NDArray[np.float64]
type SharedSurface = tuple[ArrF64, ArrF64, ArrF64, ArrF64]


def as_f64(buf: array[float], shape: tuple[int, ...], label: str | None = None) -> ArrF64:
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


def crop2_f64(slab: CompMass2D, trim: int, label: str, *, nonnegative: bool = False) -> ArrF64:
    view = _crop_sponge_2d_view(slab, trim)
    if not bool(np.all(np.isfinite(view))):
        raise ValueError(f"Non-finite value encountered in {label}")
    if nonnegative and np.any(view < 0.0):
        raise ValueError(f"Negative value encountered in {label}")
    return view.astype(np.float64)


def groom_2d(values: ArrF64, keepbits: int) -> PubMass2D:
    ny, nx = values.shape
    return PubMass2D(Slab2DF32(ny, nx, groom_f32_buf(values, keepbits)))


def _validate_and_groom_3d_pressure(
    raw: RawWrfStepInput, psfc_2d: ArrF64, trim: int, k_exp: int
) -> tuple[ArrF64, array[float]]:
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
    raw: RawWrfStepInput, trim: int, first_height: ArrF64, first_temp: ArrF64, first_qv: ArrF64
) -> PubMass2D:
    mslp = (
        np.zeros_like(first_height)
        if raw.afwa_mslp_pa is None
        else crop2_f64(raw.afwa_mslp_pa, trim, "AFWA_MSLP")
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
    return groom_2d(mslp, 17)


def _cloud_and_thermo(
    raw: RawWrfStepInput, domain: DomainProfile, science: ScienceProfile, shared_2d: SharedSurface
) -> tuple[
    ArrF64,
    array[float],
    array[float],
    array[float],
    ArrF64,
    ArrF64,
    dict[str, PubMass2D],
]:
    _, psfc, _, _ = shared_2d
    trim, k_exp = domain.bdy_trim_cells, domain.exported_levels_k
    ny, nx = psfc.shape
    shp3 = (k_exp, ny, nx)
    p_all, p_buf = _validate_and_groom_3d_pressure(raw, psfc, trim, k_exp)
    cloud_shape = (raw.cldfra.nz, ny, nx)
    # Coupled MYNN radiation stores an older CLDFRA_BL snapshot; use its current total.
    # https://github.com/wrf-model/WRF/blob/v4.8.0/phys/module_radiation_driver.F#L1403-L1416
    cloud_source = raw.cldfra
    if science.bl_pbl_physics == 5 and science.icloud_bl > 0 and raw.cldfra_bl is not None:
        cloud_source = raw.cldfra_bl
    cloud = _mass_view(cloud_source, trim, cloud_shape)
    cld_tot, cld_low = max_random_cloud_cover_pct(
        cloud, p_all, _crop_sponge_2d_view(raw.psfc_pa, trim)
    )
    p_pa = p_all[:k_exp]
    ph = np.frombuffer(crop_destagger_z_3d(raw.ph_stag_m2s2, trim, k_exp).buf, np.float32)
    phb = np.frombuffer(crop_destagger_z_3d(raw.phb_stag_m2s2, trim, k_exp).buf, np.float32)
    height_m = np.add(ph, phb, dtype=np.float64).reshape(shp3) / G0_MPS2
    z_buf = groom_f32_buf(height_m, 17)
    th = _mass_view(raw.theta_pert_k, trim, shp3).astype(np.float64)
    tk = potential_to_temp_vec(th, p_pa)
    t_buf = groom_f32_buf(tk, 15)
    validate_thermodynamic_field("temp_k", np.frombuffer(t_buf, np.float32))
    qv_in = _mass_view(raw.qvapor_kgkg, trim, shp3).astype(np.float64)
    if bool(np.any(qv_in < -1e-4)):
        raise ValueError("Large negative QVAPOR encountered in post_frame")
    spfh = mixing_ratio_to_spfh_vec(qv_in)
    if bool(np.any(spfh >= 1.0)):
        raise ValueError("Invalid specific humidity in 3D column")
    columns = {
        "cloud_total_pct": groom_2d(cld_tot, 15),
        "cloud_low_pct": groom_2d(cld_low, 15),
        "mslp_pa": _post_mslp(raw, trim, height_m[0], tk[0], qv_in[0]),
    }
    return p_pa, p_buf, z_buf, t_buf, tk, spfh, columns


def _wind_stats(
    raw: RawWrfStepInput,
    domain: DomainProfile,
    science: ScienceProfile,
    shared_2d: SharedSurface,
    thermo: tuple[ArrF64, array[float], ArrF64, ArrF64],
) -> tuple[ArrF64, ArrF64, ArrF64, tuple[float, float, int, float]]:
    terr, _, ca, sa = shared_2d
    p_pa, z_buf, tk, spfh = thermo
    trim, k_exp = domain.bdy_trim_cells, domain.exported_levels_k
    ny, nx = ca.shape
    shp3 = (k_exp, ny, nx)
    w_m = as_f64(crop_destagger_z_3d(raw.w_stag_mps, trim, k_exp).buf, shp3)
    omega = geometric_w_to_omega_vec(w_m, p_pa, tk, spfh)
    u_g = as_f64(crop_destagger_x_3d(raw.u_stag_mps, trim, k_exp).buf, shp3)
    v_g = as_f64(crop_destagger_y_3d(raw.v_stag_mps, trim, k_exp).buf, shp3)
    u_e, v_e = rotate_grid_to_earth_vec(u_g, v_g, ca, sa)
    agl = np.subtract(np.frombuffer(z_buf, np.float32).reshape(shp3), terr)
    dz, bl_n = model_column_spacing(agl)
    # True Eulerian thickness differs from the consumer midpoint gap used for admission.
    phi = staggered_face_phi(raw, trim, k_exp)
    dz_cell = np.maximum(1.0, (phi[1:] - phi[:-1]) / G0_MPS2)
    # dx_phys = dx / m, so the Courant proxy needs a Lambert scale (center-column proxy).
    m_scale = lambert_map_scale(
        domain.center_lat_deg,
        std_parallel_1_deg=domain.std_parallel_1_deg,
        std_parallel_2_deg=domain.std_parallel_2_deg,
        origin_lat_deg=domain.center_lat_deg,
        radius_m=domain.earth_radius_m,
    )
    dt_s = float(science.time_step_s if raw.dt_s is None else raw.dt_s)
    cfl = ((np.abs(u_g) + np.abs(v_g)) * m_scale / domain.dx_m + np.abs(w_m) / dz_cell) * dt_s
    stats = (float(np.min(dz)), float(np.max(dz)), bl_n, round(float(np.max(cfl)), 5))
    return u_e, v_e, omega, stats


def post_3d_slabs(
    raw: RawWrfStepInput,
    domain: DomainProfile,
    science: ScienceProfile,
    shared_2d: SharedSurface,
) -> tuple[dict[str, PubMass3D], dict[str, PubMass2D], tuple[float, float, int, float]]:
    p_pa, p_buf, z_buf, t_buf, tk, spfh, columns = _cloud_and_thermo(
        raw, domain, science, shared_2d
    )
    u_e, v_e, omega, stats = _wind_stats(raw, domain, science, shared_2d, (p_pa, z_buf, tk, spfh))
    k_exp = domain.exported_levels_k
    ny, nx = shared_2d[1].shape
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
    return f3d, columns, stats


__all__ = ["SharedSurface", "as_f64", "crop2_f64", "groom_2d", "post_3d_slabs"]
