"""Typed local NetCDF4 baseline and full-step reader for WRF computational grids."""

from __future__ import annotations

from array import array
from collections.abc import Mapping
from pathlib import Path

import numpy as np

from av_wrf.codecs.wrf_nc_grid import (
    extract_clean_f32_nc,
    extract_clean_i32_nc,
    parse_wrf_timestamp,
    rotation_sine_needs_flip,
    slice_nc_var,
    verify_local_wrf_nc,
    verify_wrfout_grid_against_domain,
)
from av_wrf.contracts import (
    CompBucket2D,
    CompMass2D,
    CompMass3D,
    CompStagX3D,
    CompStagY3D,
    CompStagZ3D,
    DomainProfile,
    RawWrfBaselineInput,
    RawWrfStepInput,
    ScienceProfile,
    Slab2DF32,
    Slab2DI32,
    Slab3DF32,
)
from av_wrf.core import MYNN_CLOUD_FIELD, verify_t_thm_consistency

RRTMG_SHORTWAVE_TOLERANCE_WM2 = 0.01


def _f32_buf(arr: np.ndarray[tuple[int, ...], np.dtype[np.float32]]) -> array[float]:
    buf = array("f")
    buf.frombytes(memoryview(arr).cast("B"))
    return buf


def _i32_buf(arr: np.ndarray[tuple[int, ...], np.dtype[np.int32]]) -> array[int]:
    buf = array("i")
    buf.frombytes(memoryview(arr).cast("B"))
    return buf


def _r2_nc(v: Mapping[str, object], p: Path, nm: str, t: int, ny: int, nx: int) -> CompMass2D:
    return CompMass2D(Slab2DF32(ny, nx, _f32_buf(extract_clean_f32_nc(v, p, nm, t, (ny, nx)))))


def _ri_nc(v: Mapping[str, object], p: Path, nm: str, t: int, ny: int, nx: int) -> CompBucket2D:
    return CompBucket2D(Slab2DI32(ny, nx, _i32_buf(extract_clean_i32_nc(v, p, nm, t, (ny, nx)))))


def _r3_nc(
    v: Mapping[str, object], p: Path, nm: str, t: int, nz: int, ny: int, nx: int, *, crop: bool
) -> CompMass3D:
    z_stop = nz if crop else None
    arr = extract_clean_f32_nc(v, p, nm, t, (nz, ny, nx), z_stop=z_stop)
    return CompMass3D(Slab3DF32(nz, ny, nx, _f32_buf(arr)))


def _rz_nc(
    v: Mapping[str, object], p: Path, nm: str, t: int, nz_faces: int, ny: int, nx: int
) -> CompStagZ3D:
    arr = extract_clean_f32_nc(v, p, nm, t, (nz_faces, ny, nx), z_stop=nz_faces)
    return CompStagZ3D(Slab3DF32(nz_faces, ny, nx, _f32_buf(arr)))


def _verify_surface_shortwave(
    v: Mapping[str, object],
    nc_path: Path,
    time_index: int,
    shape: tuple[int, int],
    science: ScienceProfile | None,
    *,
    required: bool,
) -> None:
    if science is None or science.ra_sw_physics != 4:
        return
    missing = [name for name in ("SWDNB", "SWUPB", "ALBEDO") if name not in v]
    label = f"{nc_path}: RRTMG surface shortwave [t={time_index}]"
    if missing:
        if required:
            raise ValueError(f"{label} missing {', '.join(missing)}")
        return
    down = extract_clean_f32_nc(v, nc_path, "SWDNB", time_index, shape)
    up = extract_clean_f32_nc(v, nc_path, "SWUPB", time_index, shape)
    tol_wm2 = RRTMG_SHORTWAVE_TOLERANCE_WM2
    down_min, up_min = float(down.min()), float(up.min())
    if min(down_min, up_min) < -tol_wm2:
        raise ValueError(f"{label} negative flux (SWDNB {down_min:g}, SWUPB {up_min:g} W/m²)")
    excess = float(np.max(up - down))
    if excess > tol_wm2:
        raise ValueError(f"{label} SWUPB exceeds SWDNB by {excess:g} W/m²")
    lit = down > tol_wm2
    if np.any(lit):
        albedo = np.ma.asarray(slice_nc_var(v, "ALBEDO", time_index))
        if albedo.shape != shape or albedo.dtype.kind != "f":
            raise ValueError(f"{label} ALBEDO expected float shape {shape}")
        values = albedo[lit]
        if np.ma.is_masked(values) or not 0.0 <= float(values.min()) <= float(values.max()) <= 1.0:
            raise ValueError(f"{label} daylight ALBEDO outside [0, 1] or masked")


def read_wrfout_baseline_nc(
    nc_path: Path,
    domain: DomainProfile,
    *,
    time_index: int = 0,
    expected_valid_utc: str | None = None,
    science: ScienceProfile | None = None,
    require_surface_shortwave: bool = False,
) -> RawWrfBaselineInput:
    """Read 2D accumulator baseline and verify grid and available RRTMG surface shortwave."""
    verify_local_wrf_nc(nc_path)
    import netCDF4

    ny, nx = domain.e_sn - 1, domain.e_we - 1
    with netCDF4.Dataset(str(nc_path), mode="r") as ds:
        verify_wrfout_grid_against_domain(ds, domain, nc_path, time_index, science=science)
        raw_t = bytes(ds.variables["Times"][time_index].tobytes()).decode("ascii")
        iso_utc, unix_s = parse_wrf_timestamp(raw_t)
        if expected_valid_utc is not None and iso_utc != expected_valid_utc:
            raise ValueError(f"wrfout Times {iso_utc} != expected {expected_valid_utc}")
        v = ds.variables
        _verify_surface_shortwave(
            v, nc_path, time_index, (ny, nx), science, required=require_surface_shortwave
        )
        s2 = {
            k: _r2_nc(v, nc_path, k, time_index, ny, nx)
            for k in ("HGT", "PSFC", "ACHFX", "ACLHF", "RAINC", "RAINNC")
        }
        return RawWrfBaselineInput(
            iso_utc,
            unix_s,
            s2["HGT"],
            s2["PSFC"],
            s2["ACHFX"],
            s2["ACLHF"],
            s2["RAINC"],
            _ri_nc(v, nc_path, "I_RAINC", time_index, ny, nx),
            s2["RAINNC"],
            _ri_nc(v, nc_path, "I_RAINNC", time_index, ny, nx),
        )


def _read_3d_fields(
    v: Mapping[str, object],
    nc_path: Path,
    t_i: int,
    dims: tuple[int, int, int, int],
    sc: ScienceProfile,
) -> tuple[
    CompMass3D,
    CompMass3D,
    CompMass3D,
    CompMass3D,
    CompMass3D,
    CompStagZ3D,
    CompStagZ3D,
    CompStagX3D,
    CompStagY3D,
    CompStagZ3D,
]:
    ny, nx, k_exp, nz_full = dims
    p_pert = _r3_nc(v, nc_path, "P", t_i, nz_full, ny, nx, crop=False)
    pb = _r3_nc(v, nc_path, "PB", t_i, nz_full, ny, nx, crop=False)
    t_dry = _r3_nc(v, nc_path, "T", t_i, k_exp, ny, nx, crop=True)
    qv = _r3_nc(v, nc_path, "QVAPOR", t_i, k_exp, ny, nx, crop=True)
    if "THM" not in v:
        raise ValueError(f"Missing required WRF v4 'THM' variable in {nc_path}")
    verify_t_thm_consistency(
        t_dry, _r3_nc(v, nc_path, "THM", t_i, k_exp, ny, nx, crop=True), p_pert, pb, qv, sc
    )
    u_arr = extract_clean_f32_nc(v, nc_path, "U", t_i, (k_exp, ny, nx + 1), z_stop=k_exp)
    v_arr = extract_clean_f32_nc(v, nc_path, "V", t_i, (k_exp, ny + 1, nx), z_stop=k_exp)
    return (
        p_pert,
        pb,
        t_dry,
        qv,
        _r3_nc(v, nc_path, "CLDFRA", t_i, nz_full, ny, nx, crop=False),
        _rz_nc(v, nc_path, "PH", t_i, k_exp + 1, ny, nx),
        _rz_nc(v, nc_path, "PHB", t_i, k_exp + 1, ny, nx),
        CompStagX3D(Slab3DF32(k_exp, ny, nx + 1, _f32_buf(u_arr))),
        CompStagY3D(Slab3DF32(k_exp, ny + 1, nx, _f32_buf(v_arr))),
        _rz_nc(v, nc_path, "W", t_i, k_exp + 1, ny, nx),
    )


def _read_2d_fields(
    v: Mapping[str, object],
    nc_path: Path,
    t_i: int,
    ny: int,
    nx: int,
) -> tuple[dict[str, CompMass2D], CompMass2D | None, CompBucket2D, CompBucket2D]:
    if "AFWA_CAPE_MU" not in v:
        raise ValueError(
            f"Missing required 'AFWA_CAPE_MU' in {nc_path} (enable afwa_buoy_opt=1; "
            "silent fallback to surface AFWA_CAPE is forbidden)"
        )
    afwa_mslp = _r2_nc(v, nc_path, "AFWA_MSLP", t_i, ny, nx) if "AFWA_MSLP" in v else None
    keys = (
        "HGT",
        "COSALPHA",
        "SINALPHA",
        "PSFC",
        "T2",
        "Q2",
        "U10",
        "V10",
        "HFX",
        "LH",
        "UST",
        "PBLH",
        "ACHFX",
        "ACLHF",
        "RAINC",
        "RAINNC",
        "AFWA_CAPE_MU",
    )
    s2 = {k: _r2_nc(v, nc_path, k, t_i, ny, nx) for k in keys}
    return (
        s2,
        afwa_mslp,
        _ri_nc(v, nc_path, "I_RAINC", t_i, ny, nx),
        _ri_nc(v, nc_path, "I_RAINNC", t_i, ny, nx),
    )


def _assemble_step_from_vars(
    v: Mapping[str, object],
    nc_path: Path,
    stamp: tuple[str, int],
    t_i: int,
    dims: tuple[int, int, int, int],
    science: ScienceProfile,
    dt_s: float | None = None,
) -> RawWrfStepInput:
    ny, nx, _, _ = dims
    p_p, pb, t_d, qv, cld, ph, phb, u_s, v_s, w_s = _read_3d_fields(v, nc_path, t_i, dims, science)
    s2, afwa_mslp, i_rc, i_rn = _read_2d_fields(v, nc_path, t_i, ny, nx)
    return RawWrfStepInput(
        stamp[0],
        stamp[1],
        s2["HGT"],
        s2["COSALPHA"],
        s2["SINALPHA"],
        s2["PSFC"],
        s2["T2"],
        s2["Q2"],
        s2["U10"],
        s2["V10"],
        s2["HFX"],
        s2["LH"],
        s2["UST"],
        s2["PBLH"],
        s2["AFWA_CAPE_MU"],
        s2["ACHFX"],
        s2["ACLHF"],
        s2["RAINC"],
        i_rc,
        s2["RAINNC"],
        i_rn,
        p_p,
        pb,
        ph,
        phb,
        t_d,
        qv,
        cld,
        u_s,
        v_s,
        w_s,
        afwa_mslp_pa=afwa_mslp,
        dt_s=dt_s,
        cldfra_bl=_r3_nc(v, nc_path, MYNN_CLOUD_FIELD, t_i, dims[3], ny, nx, crop=False)
        if MYNN_CLOUD_FIELD in v
        else None,
    )


def read_wrfout_step_nc(
    nc_path: Path,
    domain: DomainProfile,
    science: ScienceProfile,
    *,
    time_index: int = 0,
    expected_valid_utc: str | None = None,
    require_surface_shortwave: bool = False,
    require_mynn_clouds: bool = False,
) -> RawWrfStepInput:
    """Read one wrfout time and check native radiation and MYNN fields when required."""
    verify_local_wrf_nc(nc_path)
    import netCDF4

    dims = (domain.e_sn - 1, domain.e_we - 1, domain.exported_levels_k, domain.e_vert - 1)
    with netCDF4.Dataset(str(nc_path), mode="r") as ds:
        xlong = verify_wrfout_grid_against_domain(ds, domain, nc_path, time_index, science=science)
        raw_t = bytes(ds.variables["Times"][time_index].tobytes()).decode("ascii")
        stamp = parse_wrf_timestamp(raw_t)
        if expected_valid_utc is not None and stamp[0] != expected_valid_utc:
            raise ValueError(f"wrfout Times {stamp[0]} != expected {expected_valid_utc}")
        required_clouds = (
            require_mynn_clouds and science.bl_pbl_physics == 5 and science.icloud_bl > 0
        )
        if required_clouds and MYNN_CLOUD_FIELD not in ds.variables:
            raise ValueError(f"{nc_path}: Missing required MYNN history field {MYNN_CLOUD_FIELD}")
        _verify_surface_shortwave(
            ds.variables,
            nc_path,
            time_index,
            (dims[0], dims[1]),
            science,
            required=require_surface_shortwave,
        )
        if require_surface_shortwave and science.ra_sw_physics == 4:
            slope = extract_clean_f32_nc(ds.variables, nc_path, "SWDOWN", time_index, dims[:2])
            if not -1.0 <= slope.min() <= slope.max() <= 1500.0:
                raise ValueError(f"{nc_path}: slope-corrected SWDOWN outside [-1, 1500] W/m²")
        dt_s = float(ds.getncattr("DT")) if "DT" in ds.ncattrs() else None
        if dt_s is not None and (not np.isfinite(dt_s) or dt_s <= 0):
            raise ValueError("WRF DT must be finite and positive")
        step = _assemble_step_from_vars(
            ds.variables, nc_path, stamp, time_index, dims, science, dt_s=dt_s
        )
    cosine = np.frombuffer(step.cos_alpha.buf, np.float32).reshape(xlong.shape)
    sine = np.frombuffer(step.sin_alpha.buf, np.float32).reshape(xlong.shape)
    if rotation_sine_needs_flip(domain, xlong, cosine, sine, str(nc_path)):
        sine *= -1.0
    return step


__all__ = [
    "RRTMG_SHORTWAVE_TOLERANCE_WM2",
    "RawWrfBaselineInput",
    "RawWrfStepInput",
    "read_wrfout_baseline_nc",
    "read_wrfout_step_nc",
    "verify_local_wrf_nc",
    "verify_wrfout_grid_against_domain",
]
