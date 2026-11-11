"""Typed local NetCDF4 reader and Times-indexed wrfout slice selector."""

from __future__ import annotations

from array import array
from collections.abc import Mapping
from pathlib import Path

from av_wrf.contracts import (
    CompBucket2D,
    CompMass2D,
    CompMass3D,
    CompStagX3D,
    CompStagY3D,
    CompStagZ3D,
    DomainProfile,
    RawWrfStepInput,
    ScienceProfile,
    Slab2DF32,
    Slab2DI32,
    Slab3DF32,
    format_iso_utc,
    parse_iso_utc,
)
from av_wrf.core import verify_t_thm_consistency


def verify_local_wrf_nc(nc_path: Path) -> str:
    """Verify local wrfout/wrfinput file exists before NetCDF4 handle open."""
    if not nc_path.is_file():
        raise FileNotFoundError(f"Missing local WRF NetCDF file: {nc_path}")
    return nc_path.name


def _parse_wrf_timestamp(raw_chars: str) -> tuple[str, int]:
    cleaned = raw_chars.strip().replace("_", "T") + "Z"
    dt = parse_iso_utc(cleaned)
    return format_iso_utc(dt), int(dt.timestamp())


def select_cycle_wrfout_slices(
    wrfout_dir: Path,
    publish_origin_utc: str,
    valid_times_utc: tuple[str, ...],
) -> tuple[tuple[Path, int], tuple[tuple[Path, int], ...]]:
    """Index `wrfout_d01_*` by `Times` and return exact `(f000, lead_slices)` in UTC order."""
    import netCDF4

    index: dict[str, tuple[Path, int]] = {}
    for nc_path in sorted(wrfout_dir.glob("wrfout_d01_*")):
        verify_local_wrf_nc(nc_path)
        with netCDF4.Dataset(str(nc_path), mode="r") as ds:
            if "Times" not in ds.variables:
                raise ValueError(f"Missing 'Times' variable in {nc_path}")
            times_var = ds.variables["Times"]
            for t_idx in range(int(times_var.shape[0])):
                raw_t = bytes(times_var[t_idx].tobytes()).decode("ascii")
                iso_utc, _ = _parse_wrf_timestamp(raw_t)
                if iso_utc in index:
                    raise ValueError(f"Duplicate wrfout timestamp {iso_utc} in {nc_path}")
                index[iso_utc] = (nc_path, t_idx)

    if publish_origin_utc not in index:
        raise ValueError(f"Missing f000 wrfout frame for publish_origin_utc {publish_origin_utc}")
    missing = [vt for vt in valid_times_utc if vt not in index]
    if missing:
        raise ValueError(f"Missing wrfout lead timestamps: {', '.join(missing[:6])}")
    return index[publish_origin_utc], tuple(index[vt] for vt in valid_times_utc)


def _f32_buf_from_slice(sliced: object, expected_shape: tuple[int, ...]) -> array[float]:
    import numpy as np

    arr = np.asarray(sliced)
    if arr.shape != expected_shape:
        raise ValueError(f"WRF variable shape {arr.shape} != expected {expected_shape}")
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


def _slice_var(variables: Mapping[str, object], name: str, time_index: int) -> object:
    var_obj = variables[name]
    getter = getattr(var_obj, "__getitem__", None)
    if getter is None:
        raise TypeError(f"NetCDF variable '{name}' is not indexable")
    return getter(time_index)


def _r2_nc(variables: Mapping[str, object], name: str, t_idx: int, ny: int, nx: int) -> CompMass2D:
    buf = _f32_buf_from_slice(_slice_var(variables, name, t_idx), (ny, nx))
    return CompMass2D(Slab2DF32(ny, nx, buf))


def _ri_nc(
    variables: Mapping[str, object], name: str, t_idx: int, ny: int, nx: int
) -> CompBucket2D:
    buf = _i32_buf_from_slice(_slice_var(variables, name, t_idx), (ny, nx))
    return CompBucket2D(Slab2DI32(ny, nx, buf))


def _r3_nc(
    variables: Mapping[str, object], name: str, t_idx: int, nz: int, ny: int, nx: int
) -> CompMass3D:
    buf = _f32_buf_from_slice(_slice_var(variables, name, t_idx), (nz, ny, nx))
    return CompMass3D(Slab3DF32(nz, ny, nx, buf))


def _rz_nc(
    variables: Mapping[str, object], name: str, t_idx: int, nz: int, ny: int, nx: int
) -> CompStagZ3D:
    buf = _f32_buf_from_slice(_slice_var(variables, name, t_idx), (nz + 1, ny, nx))
    return CompStagZ3D(Slab3DF32(nz + 1, ny, nx, buf))


def _assemble_step_from_vars(
    variables: Mapping[str, object],
    nc_path: Path,
    iso_utc: str,
    unix_s: int,
    t_i: int,
    ny: int,
    nx: int,
    nz: int,
    science: ScienceProfile,
) -> RawWrfStepInput:
    p_pert = _r3_nc(variables, "P", t_i, nz, ny, nx)
    pb = _r3_nc(variables, "PB", t_i, nz, ny, nx)
    t_dry = _r3_nc(variables, "T", t_i, nz, ny, nx)
    qv = _r3_nc(variables, "QVAPOR", t_i, nz, ny, nx)
    if "THM" not in variables:
        raise ValueError(f"Missing required WRF v4 'THM' variable in {nc_path}")
    verify_t_thm_consistency(
        t_dry, _r3_nc(variables, "THM", t_i, nz, ny, nx), p_pert, pb, qv, science
    )
    u_b = _f32_buf_from_slice(_slice_var(variables, "U", t_i), (nz, ny, nx + 1))
    v_b = _f32_buf_from_slice(_slice_var(variables, "V", t_i), (nz, ny + 1, nx))
    return RawWrfStepInput(
        valid_time_utc=iso_utc,
        valid_time_unix_s=unix_s,
        terrain_m=_r2_nc(variables, "HGT", t_i, ny, nx),
        cos_alpha=_r2_nc(variables, "COSALPHA", t_i, ny, nx),
        sin_alpha=_r2_nc(variables, "SINALPHA", t_i, ny, nx),
        psfc_pa=_r2_nc(variables, "PSFC", t_i, ny, nx),
        t2_k=_r2_nc(variables, "T2", t_i, ny, nx),
        q2_kgkg=_r2_nc(variables, "Q2", t_i, ny, nx),
        u10_grid_mps=_r2_nc(variables, "U10", t_i, ny, nx),
        v10_grid_mps=_r2_nc(variables, "V10", t_i, ny, nx),
        wspd10max_mps=_r2_nc(variables, "WSPD10MAX", t_i, ny, nx),
        ustar_mps=_r2_nc(variables, "UST", t_i, ny, nx),
        pblh_m=_r2_nc(variables, "PBLH", t_i, ny, nx),
        afwa_cape_jkg=_r2_nc(variables, "AFWA_CAPE", t_i, ny, nx),
        achfx_j_m2=_r2_nc(variables, "ACHFX", t_i, ny, nx),
        i_achfx=_ri_nc(variables, "I_ACHFX", t_i, ny, nx),
        aclhf_j_m2=_r2_nc(variables, "ACLHF", t_i, ny, nx),
        i_aclhf=_ri_nc(variables, "I_ACLHF", t_i, ny, nx),
        rainc_mm=_r2_nc(variables, "RAINC", t_i, ny, nx),
        i_rainc=_ri_nc(variables, "I_RAINC", t_i, ny, nx),
        rainnc_mm=_r2_nc(variables, "RAINNC", t_i, ny, nx),
        i_rainnc=_ri_nc(variables, "I_RAINNC", t_i, ny, nx),
        p_pert_pa=p_pert,
        pb_pa=pb,
        ph_stag_m2s2=_rz_nc(variables, "PH", t_i, nz, ny, nx),
        phb_stag_m2s2=_rz_nc(variables, "PHB", t_i, nz, ny, nx),
        theta_pert_k=t_dry,
        qvapor_kgkg=qv,
        cldfra=_r3_nc(variables, "CLDFRA", t_i, nz, ny, nx),
        u_stag_mps=CompStagX3D(Slab3DF32(nz, ny, nx + 1, u_b)),
        v_stag_mps=CompStagY3D(Slab3DF32(nz, ny + 1, nx, v_b)),
        w_stag_mps=_rz_nc(variables, "W", t_i, nz, ny, nx),
    )


def read_wrfout_step_nc(
    nc_path: Path,
    domain: DomainProfile,
    science: ScienceProfile,
    *,
    time_index: int = 0,
    expected_valid_utc: str | None = None,
) -> RawWrfStepInput:
    """Read and validate one computational-grid time slice from a local wrfout NetCDF4 file."""
    verify_local_wrf_nc(nc_path)
    import netCDF4

    ny, nx, nz = domain.e_sn - 1, domain.e_we - 1, domain.e_vert - 1
    with netCDF4.Dataset(str(nc_path), mode="r") as ds:
        raw_t = bytes(ds.variables["Times"][time_index].tobytes()).decode("ascii")
        iso_utc, unix_s = _parse_wrf_timestamp(raw_t)
        if expected_valid_utc is not None and iso_utc != expected_valid_utc:
            raise ValueError(f"wrfout Times {iso_utc} != expected {expected_valid_utc}")
        return _assemble_step_from_vars(
            ds.variables, nc_path, iso_utc, unix_s, time_index, ny, nx, nz, science
        )


__all__ = [
    "RawWrfStepInput",
    "read_wrfout_step_nc",
    "select_cycle_wrfout_slices",
    "verify_local_wrf_nc",
]
