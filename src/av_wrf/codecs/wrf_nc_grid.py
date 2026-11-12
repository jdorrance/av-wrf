"""WRF NetCDF `Times` indexing, mask/fill rejection, and Lambert/ZNW verification."""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import numpy.typing as npt

from av_wrf.contracts import DomainProfile, ScienceProfile, format_iso_utc, parse_iso_utc
from av_wrf.core import (
    computational_grid_origin_xy_m,
    lambert_xy_m_to_latlon,
    validate_solver_znw,
    validate_wrfinput_columns,
)

NC_FLOAT_FILL_THRESHOLD = 1.0e30
GRID_COORD_TOL_DEG = 5.0e-4


def verify_local_wrf_nc(nc_path: Path) -> str:
    """Verify local wrfout/wrfinput file exists before NetCDF4 handle open."""
    if not nc_path.is_file():
        raise FileNotFoundError(f"Missing local WRF NetCDF file: {nc_path}")
    return nc_path.name


def parse_wrf_timestamp(raw_chars: str) -> tuple[str, int]:
    """Parse WRF `YYYY-MM-DD_HH:MM:SS` character stamp into canonical ISO-8601 UTC + epoch."""
    dt = parse_iso_utc(raw_chars.strip().replace("_", "T") + "Z")
    return format_iso_utc(dt), int(dt.timestamp())


def _reject_masked(sliced: object, nc_path: Path, name: str, t_idx: int) -> None:
    if np.ma.isMaskedArray(sliced) and bool(np.ma.is_masked(sliced)):
        mask = np.ma.getmaskarray(sliced)
        bad = tuple(int(x) for x in np.argwhere(mask)[0])
        raise ValueError(f"{nc_path}:{name}[t={t_idx}]: masked NetCDF cell at {bad}")


def slice_nc_var(
    variables: Mapping[str, object], name: str, time_index: int, *, z_stop: int | None = None
) -> object:
    """Index time slice `time_index` (and optional `:z_stop` vertical hyperslab) from NetCDF4."""
    if name not in variables:
        raise ValueError(f"Missing required WRF NetCDF variable '{name}'")
    var_obj = variables[name]
    getter = getattr(var_obj, "__getitem__", None)
    if getter is None:
        raise TypeError(f"NetCDF variable '{name}' is not indexable")
    return getter(time_index if z_stop is None else (time_index, slice(0, z_stop)))


def extract_clean_f32_nc(
    variables: Mapping[str, object],
    nc_path: Path,
    name: str,
    t_idx: int,
    expected_shape: tuple[int, ...],
    *,
    z_stop: int | None = None,
) -> npt.NDArray[np.float32]:
    """Slice NetCDF float variable and reject masks, `_FillValue`, `>=1e30` sentinels, or NaN."""
    sliced = slice_nc_var(variables, name, t_idx, z_stop=z_stop)
    _reject_masked(sliced, nc_path, name, t_idx)
    arr = np.asarray(sliced)
    if arr.shape != expected_shape or arr.dtype.kind != "f":
        raise ValueError(f"{nc_path}:{name}[t={t_idx}] expected float shape {expected_shape}")
    valid = (arr > -NC_FLOAT_FILL_THRESHOLD) & (arr < NC_FLOAT_FILL_THRESHOLD)
    if not bool(np.all(valid)):
        bad = tuple(int(x) for x in np.argwhere(~valid)[0])
        raise ValueError(
            f"{nc_path}:{name}[t={t_idx}]: fill or non-finite value {arr[bad]} at {bad}"
        )
    return np.ascontiguousarray(arr, dtype=np.float32)


def extract_clean_i32_nc(
    variables: Mapping[str, object],
    nc_path: Path,
    name: str,
    t_idx: int,
    expected_shape: tuple[int, ...],
) -> npt.NDArray[np.int32]:
    """Slice NetCDF int32 bucket counter and reject masks, negative counts, or fill sentinels."""
    sliced = slice_nc_var(variables, name, t_idx)
    _reject_masked(sliced, nc_path, name, t_idx)
    arr = np.asarray(sliced)
    if arr.shape != expected_shape or arr.dtype.kind not in "iu":
        raise ValueError(f"{nc_path}:{name}[t={t_idx}] expected integer shape {expected_shape}")
    bad_mask = (arr < 0) | (arr > np.iinfo(np.int32).max)
    if bool(np.any(bad_mask)):
        bad = tuple(int(x) for x in np.argwhere(bad_mask)[0])
        raise ValueError(f"{nc_path}:{name}[t={t_idx}]: invalid bucket count {arr[bad]} at {bad}")
    return np.ascontiguousarray(arr, dtype=np.int32)


def _index_wrfout_times(wrfout_dir: Path) -> dict[str, tuple[Path, int]]:
    import netCDF4

    index: dict[str, tuple[Path, int]] = {}
    for nc_path in sorted(wrfout_dir.glob("wrfout_d01_*")):
        verify_local_wrf_nc(nc_path)
        with netCDF4.Dataset(str(nc_path), mode="r") as ds:
            if "Times" not in ds.variables:
                raise ValueError(f"Missing 'Times' variable in {nc_path}")
            times_var = ds.variables["Times"]
            for t_idx in range(int(times_var.shape[0])):
                raw_s = bytes(times_var[t_idx].tobytes()).decode("ascii")
                iso_utc, _ = parse_wrf_timestamp(raw_s)
                if iso_utc in index:
                    raise ValueError(f"Duplicate wrfout timestamp {iso_utc} in {nc_path}")
                index[iso_utc] = (nc_path, t_idx)
    return index


def select_cycle_wrfout_slices(
    wrfout_dir: Path, publish_origin_utc: str, valid_times_utc: tuple[str, ...]
) -> tuple[tuple[Path, int], tuple[tuple[Path, int], ...]]:
    """Index `wrfout_d01_*` by `Times` and return exact `(f000, lead_slices)` in UTC order."""
    index = _index_wrfout_times(wrfout_dir)
    if publish_origin_utc not in index:
        raise ValueError(f"Missing f000 wrfout frame for publish_origin_utc {publish_origin_utc}")
    missing = [vt for vt in valid_times_utc if vt not in index]
    if missing:
        raise ValueError(f"Missing wrfout lead timestamps: {', '.join(missing[:6])}")
    return index[publish_origin_utc], tuple(index[vt] for vt in valid_times_utc)


def _check_global_grid_attrs(ds: object, domain: DomainProfile, nc_path: Path) -> None:
    for attr_name, expected, tol in (
        ("MAP_PROJ", 1.0, 0.0),
        ("DX", domain.dx_m, 1e-2),
        ("DY", domain.dy_m, 1e-2),
        ("TRUELAT1", domain.std_parallel_1_deg, 1e-3),
        ("TRUELAT2", domain.std_parallel_2_deg, 1e-3),
        ("STAND_LON", domain.central_meridian_deg, 1e-3),
        ("CEN_LAT", domain.center_lat_deg, 1e-3),
        ("CEN_LON", domain.center_lon_deg, 1e-3),
    ):
        if not hasattr(ds, attr_name):
            raise ValueError(f"Missing global attribute '{attr_name}' in {nc_path}")
        actual = float(getattr(ds, attr_name))
        delta = actual - expected
        if attr_name in ("CEN_LON", "STAND_LON"):
            delta = (delta + 180.0) % 360.0 - 180.0
        if not math.isfinite(delta) or abs(delta) > tol:
            raise ValueError(f"{nc_path}: {attr_name}={actual} != configured {expected}")


def verify_grid_against_domain(
    ds: object,
    domain: DomainProfile,
    nc_path: Path,
    t_idx: int = 0,
    *,
    wps: bool = False,
) -> None:
    """Validate WPS or WRF Lambert grid attributes and mass-grid corner registration."""
    _check_global_grid_attrs(ds, domain, nc_path)
    variables: Mapping[str, object] = getattr(ds, "variables", {})
    ny, nx = domain.e_sn - 1, domain.e_we - 1
    lat_name, lon_name = ("XLAT_M", "XLONG_M") if wps else ("XLAT", "XLONG")
    xlat = extract_clean_f32_nc(variables, nc_path, lat_name, t_idx, (ny, nx))
    xlong = extract_clean_f32_nc(variables, nc_path, lon_name, t_idx, (ny, nx))
    x0_comp, y0_comp = computational_grid_origin_xy_m(domain)
    corners = ((0, 0), (ny - 1, 0), (0, nx - 1), (ny - 1, nx - 1))
    for jr, ir in (*corners, (ny // 2, nx // 2)):
        exp_lat, exp_lon = lambert_xy_m_to_latlon(
            x0_comp + ir * domain.dx_m,
            y0_comp + jr * domain.dy_m,
            origin_lat_deg=domain.center_lat_deg,
            central_meridian_deg=domain.central_meridian_deg,
            std_parallel_1_deg=domain.std_parallel_1_deg,
            std_parallel_2_deg=domain.std_parallel_2_deg,
            radius_m=domain.earth_radius_m,
        )
        dlon = ((float(xlong[jr, ir]) - exp_lon + 180.0) % 360.0) - 180.0
        if (
            abs(float(xlat[jr, ir]) - exp_lat) > GRID_COORD_TOL_DEG
            or abs(dlon) > GRID_COORD_TOL_DEG
        ):
            raise ValueError(
                f"{nc_path}: {lat_name}/{lon_name} at ({jr},{ir}) disagrees with DomainProfile"
            )


def verify_wrfout_grid_against_domain(
    ds: object,
    domain: DomainProfile,
    nc_path: Path,
    t_idx: int = 0,
    *,
    science: ScienceProfile | None = None,
) -> None:
    """Validate WRF grid registration, eta faces and hybrid coordinate parameters."""
    verify_grid_against_domain(ds, domain, nc_path, t_idx)
    variables: Mapping[str, object] = getattr(ds, "variables", {})
    znw_arr = extract_clean_f32_nc(variables, nc_path, "ZNW", t_idx, (domain.e_vert,))
    validate_solver_znw([float(eta) for eta in znw_arr], domain)
    if science is not None and (
        getattr(ds, "HYBRID_OPT", None) != science.hybrid_opt
        or not math.isclose(float(getattr(ds, "ETAC", math.nan)), science.etac, abs_tol=1e-6)
    ):
        raise ValueError(f"{nc_path}: HYBRID_OPT or ETAC disagrees with ScienceProfile")


def validate_wrfinput_nc(
    wrfinput_path: Path, domain: DomainProfile, science: ScienceProfile
) -> None:
    """Validate `wrfinput_d01` grid registration and `ZNW` immediately after `real.exe`."""
    verify_local_wrf_nc(wrfinput_path)
    import netCDF4

    with netCDF4.Dataset(str(wrfinput_path), mode="r") as ds:
        verify_wrfout_grid_against_domain(ds, domain, wrfinput_path, 0, science=science)
        ny, nx, n_faces = domain.e_sn - 1, domain.e_we - 1, domain.exported_levels_k + 1
        shape = (n_faces, ny, nx)
        ph = extract_clean_f32_nc(ds.variables, wrfinput_path, "PH", 0, shape, z_stop=n_faces)
        phb = extract_clean_f32_nc(ds.variables, wrfinput_path, "PHB", 0, shape, z_stop=n_faces)
        hgt = extract_clean_f32_nc(ds.variables, wrfinput_path, "HGT", 0, (ny, nx))
        validate_wrfinput_columns(ph, phb, hgt, domain)
