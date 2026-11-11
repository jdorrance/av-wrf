"""Typed local NetCDF4 baseline and full-step reader for WRF computational grids."""

from __future__ import annotations

from array import array
from collections.abc import Mapping, Sequence
from pathlib import Path

from av_wrf.codecs.wrf_nc_grid import (
    parse_wrf_timestamp,
    select_cycle_wrfout_slices,
    select_cycle_wrfout_windows,
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
from av_wrf.core import verify_t_thm_consistency


def _f32_buf_from_slice(sliced: object, expected_shape: tuple[int, ...]) -> array[float]:
    import numpy as np

    arr = np.asarray(sliced)
    if arr.shape != expected_shape or not bool(np.all(np.isfinite(arr))):
        raise ValueError(f"Invalid shape or non-finite float in wrfout slice ({arr.shape})")
    buf = array("f")
    buf.frombytes(np.ascontiguousarray(arr, dtype=np.float32).tobytes())
    return buf


def _i32_buf_from_slice(sliced: object, expected_shape: tuple[int, ...]) -> array[int]:
    import numpy as np

    arr = np.asarray(sliced)
    if arr.shape != expected_shape:
        raise ValueError(f"WRF int variable shape {arr.shape} != expected {expected_shape}")
    buf = array("i")
    buf.frombytes(np.ascontiguousarray(arr, dtype=np.int32).tobytes())
    return buf


def _r2_nc(v: Mapping[str, object], nm: str, t: int, ny: int, nx: int) -> CompMass2D:
    return CompMass2D(Slab2DF32(ny, nx, _f32_buf_from_slice(slice_nc_var(v, nm, t), (ny, nx))))


def _ri_nc(v: Mapping[str, object], nm: str, t: int, ny: int, nx: int) -> CompBucket2D:
    return CompBucket2D(Slab2DI32(ny, nx, _i32_buf_from_slice(slice_nc_var(v, nm, t), (ny, nx))))


def _r3_nc(v: Mapping[str, object], nm: str, t: int, nz: int, ny: int, nx: int) -> CompMass3D:
    b = _f32_buf_from_slice(slice_nc_var(v, nm, t), (nz, ny, nx))
    return CompMass3D(Slab3DF32(nz, ny, nx, b))


def _rz_nc(v: Mapping[str, object], nm: str, t: int, nz: int, ny: int, nx: int) -> CompStagZ3D:
    b = _f32_buf_from_slice(slice_nc_var(v, nm, t), (nz + 1, ny, nx))
    return CompStagZ3D(Slab3DF32(nz + 1, ny, nx, b))


def read_wrfout_baseline_nc(
    nc_path: Path,
    domain: DomainProfile,
    *,
    time_index: int = 0,
    expected_valid_utc: str | None = None,
) -> RawWrfBaselineInput:
    """Read minimal 2D f000 baseline fields + verify grid without loading 3D volumes."""
    verify_local_wrf_nc(nc_path)
    import netCDF4

    ny, nx = domain.e_sn - 1, domain.e_we - 1
    with netCDF4.Dataset(str(nc_path), mode="r") as ds:
        verify_wrfout_grid_against_domain(ds, domain, nc_path, time_index)
        raw_t = bytes(ds.variables["Times"][time_index].tobytes()).decode("ascii")
        iso_utc, unix_s = parse_wrf_timestamp(raw_t)
        if expected_valid_utc is not None and iso_utc != expected_valid_utc:
            raise ValueError(f"wrfout Times {iso_utc} != expected {expected_valid_utc}")
        v = ds.variables
        s2 = {
            k: _r2_nc(v, k, time_index, ny, nx)
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
            _ri_nc(v, "I_RAINC", time_index, ny, nx),
            s2["RAINNC"],
            _ri_nc(v, "I_RAINNC", time_index, ny, nx),
        )


def _window_max_wspd10(
    base_wspd: CompMass2D, extra_slices: Sequence[tuple[Path, int]], ny: int, nx: int
) -> CompMass2D:
    if not extra_slices:
        return base_wspd
    import netCDF4
    import numpy as np

    acc = np.frombuffer(base_wspd.buf, dtype=np.float32).reshape(ny, nx).copy()
    for path_i, t_i in extra_slices:
        verify_local_wrf_nc(path_i)
        with netCDF4.Dataset(str(path_i), mode="r") as ds_i:
            cur = np.asarray(slice_nc_var(ds_i.variables, "WSPD10MAX", t_i), dtype=np.float32)
            if cur.shape != (ny, nx) or not bool(np.all(np.isfinite(cur))):
                raise ValueError(f"Invalid WSPD10MAX slice in {path_i}")
            acc = np.maximum(acc, cur)
    buf = array("f")
    buf.frombytes(np.ascontiguousarray(acc, dtype=np.float32).tobytes())
    return CompMass2D(Slab2DF32(ny, nx, buf))


def _read_3d_fields(
    v: Mapping[str, object], nc_path: Path, t_i: int, dims: tuple[int, int, int], sc: ScienceProfile
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
    ny, nx, nz = dims
    p_pert, pb = _r3_nc(v, "P", t_i, nz, ny, nx), _r3_nc(v, "PB", t_i, nz, ny, nx)
    t_dry, qv = _r3_nc(v, "T", t_i, nz, ny, nx), _r3_nc(v, "QVAPOR", t_i, nz, ny, nx)
    if "THM" not in v:
        raise ValueError(f"Missing required WRF v4 'THM' variable in {nc_path}")
    verify_t_thm_consistency(t_dry, _r3_nc(v, "THM", t_i, nz, ny, nx), p_pert, pb, qv, sc)
    u_b = _f32_buf_from_slice(slice_nc_var(v, "U", t_i), (nz, ny, nx + 1))
    v_b = _f32_buf_from_slice(slice_nc_var(v, "V", t_i), (nz, ny + 1, nx))
    return (
        p_pert,
        pb,
        t_dry,
        qv,
        _r3_nc(v, "CLDFRA", t_i, nz, ny, nx),
        _rz_nc(v, "PH", t_i, nz, ny, nx),
        _rz_nc(v, "PHB", t_i, nz, ny, nx),
        CompStagX3D(Slab3DF32(nz, ny, nx + 1, u_b)),
        CompStagY3D(Slab3DF32(nz, ny + 1, nx, v_b)),
        _rz_nc(v, "W", t_i, nz, ny, nx),
    )


def _read_2d_fields(
    v: Mapping[str, object],
    t_i: int,
    ny: int,
    nx: int,
    extra_gust_slices: Sequence[tuple[Path, int]],
) -> tuple[dict[str, CompMass2D], CompMass2D | None, CompBucket2D, CompBucket2D]:
    cape_k = "AFWA_CAPE_MU" if "AFWA_CAPE_MU" in v else "AFWA_CAPE"
    afwa_mslp = _r2_nc(v, "AFWA_MSLP", t_i, ny, nx) if "AFWA_MSLP" in v else None
    s2 = {
        k: _r2_nc(v, k, t_i, ny, nx)
        for k in (
            "HGT",
            "COSALPHA",
            "SINALPHA",
            "PSFC",
            "T2",
            "Q2",
            "U10",
            "V10",
            "UST",
            "PBLH",
            "ACHFX",
            "ACLHF",
            "RAINC",
            "RAINNC",
        )
    }
    s2["CAPE"] = _r2_nc(v, cape_k, t_i, ny, nx)
    s2["WMAX"] = _window_max_wspd10(_r2_nc(v, "WSPD10MAX", t_i, ny, nx), extra_gust_slices, ny, nx)
    return s2, afwa_mslp, _ri_nc(v, "I_RAINC", t_i, ny, nx), _ri_nc(v, "I_RAINNC", t_i, ny, nx)


def _assemble_step_from_vars(
    v: Mapping[str, object],
    nc_path: Path,
    stamp: tuple[str, int],
    t_i: int,
    dims: tuple[int, int, int],
    science: ScienceProfile,
    extra_gust_slices: Sequence[tuple[Path, int]] = (),
) -> RawWrfStepInput:
    ny, nx, _ = dims
    p_p, pb, t_d, qv, cld, ph, phb, u_s, v_s, w_s = _read_3d_fields(v, nc_path, t_i, dims, science)
    s2, afwa_mslp, i_rc, i_rn = _read_2d_fields(v, t_i, ny, nx, extra_gust_slices)
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
        s2["WMAX"],
        s2["UST"],
        s2["PBLH"],
        s2["CAPE"],
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
    window_slices: Sequence[tuple[Path, int]] = (),
) -> RawWrfStepInput:
    """Read and validate one computational-grid time slice from a local wrfout NetCDF4 file."""
    verify_local_wrf_nc(nc_path)
    import netCDF4

    dims = (domain.e_sn - 1, domain.e_we - 1, domain.e_vert - 1)
    extra = [s for s in window_slices if s != (nc_path, time_index)]
    with netCDF4.Dataset(str(nc_path), mode="r") as ds:
        verify_wrfout_grid_against_domain(ds, domain, nc_path, time_index)
        raw_t = bytes(ds.variables["Times"][time_index].tobytes()).decode("ascii")
        stamp = parse_wrf_timestamp(raw_t)
        if expected_valid_utc is not None and stamp[0] != expected_valid_utc:
            raise ValueError(f"wrfout Times {stamp[0]} != expected {expected_valid_utc}")
        return _assemble_step_from_vars(
            ds.variables, nc_path, stamp, time_index, dims, science, extra
        )


__all__ = [
    "RawWrfBaselineInput",
    "RawWrfStepInput",
    "read_wrfout_baseline_nc",
    "read_wrfout_step_nc",
    "select_cycle_wrfout_slices",
    "select_cycle_wrfout_windows",
    "verify_local_wrf_nc",
    "verify_wrfout_grid_against_domain",
]
