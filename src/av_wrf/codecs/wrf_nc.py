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
    select_cycle_wrfout_slices,
    validate_wrfinput_nc,
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
from av_wrf.core import verify_t_thm_consistency


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


def read_wrfout_baseline_nc(
    nc_path: Path,
    domain: DomainProfile,
    *,
    time_index: int = 0,
    expected_valid_utc: str | None = None,
    science: ScienceProfile | None = None,
) -> RawWrfBaselineInput:
    """Read minimal 2D accumulator baseline fields + verify grid without loading 3D volumes."""
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
    s2 = {
        k: _r2_nc(v, nc_path, k, t_i, ny, nx)
        for k in (
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
    }
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
    )


def read_wrfout_step_nc(
    nc_path: Path,
    domain: DomainProfile,
    science: ScienceProfile,
    *,
    time_index: int = 0,
    expected_valid_utc: str | None = None,
) -> RawWrfStepInput:
    """Read one wrfout time with full pressure/cloud columns and export-level hyperslabs."""
    verify_local_wrf_nc(nc_path)
    import netCDF4

    dims = (domain.e_sn - 1, domain.e_we - 1, domain.exported_levels_k, domain.e_vert - 1)
    with netCDF4.Dataset(str(nc_path), mode="r") as ds:
        xlong = verify_wrfout_grid_against_domain(ds, domain, nc_path, time_index, science=science)
        raw_t = bytes(ds.variables["Times"][time_index].tobytes()).decode("ascii")
        stamp = parse_wrf_timestamp(raw_t)
        if expected_valid_utc is not None and stamp[0] != expected_valid_utc:
            raise ValueError(f"wrfout Times {stamp[0]} != expected {expected_valid_utc}")
        step = _assemble_step_from_vars(ds.variables, nc_path, stamp, time_index, dims, science)
    cosine = np.frombuffer(step.cos_alpha.buf, np.float32).reshape(xlong.shape)
    sine = np.frombuffer(step.sin_alpha.buf, np.float32).reshape(xlong.shape)
    if rotation_sine_needs_flip(domain, xlong, cosine, sine, str(nc_path)):
        sine *= -1.0
    return step


__all__ = [
    "RawWrfBaselineInput",
    "RawWrfStepInput",
    "read_wrfout_baseline_nc",
    "read_wrfout_step_nc",
    "select_cycle_wrfout_slices",
    "validate_wrfinput_nc",
    "verify_local_wrf_nc",
    "verify_wrfout_grid_against_domain",
]
