"""WRF NetCDF `Times` indexing, mask/fill rejection, and Lambert/ZNW verification."""

from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

from av_wrf.contracts import DomainProfile, ScienceProfile, format_iso_utc, parse_iso_utc
from av_wrf.core import (
    computational_grid_origin_xy_m,
    lambert_grid_to_earth_trig,
    lambert_xy_m_to_latlon,
    validate_solver_znw,
    validate_wrfinput_columns,
)

if TYPE_CHECKING:
    import netCDF4

NC_FLOAT_FILL_THRESHOLD = 1.0e30
GRID_COORD_TOL_DEG = 5.0e-4
_WPS_FIELD_LIMITS = {
    "SM": (0.0, 1.0),
    "SOILM": (0.0, 1.0),
    "SPECHUMD": (0.0, math.nextafter(1.0, 0.0)),
}
_WPS_FIELD_FLAGS = {"SOIL_LEVELS": "FLAG_SOIL_LEVELS", "SPECHUMD": "FLAG_SH"}


def verify_wps_fields(ds: netCDF4.Dataset, path: Path, fields: tuple[str, ...]) -> None:
    """Check required WPS fields, WRF activation flags, and physical value bounds."""
    import netCDF4

    for name in fields:
        var = ds.variables.get(name)
        if not isinstance(var, netCDF4.Variable):
            raise ValueError(f"Missing required WPS field {name} in {path}")
        # real.exe uses FLAG_SH and converts specific humidity as q / (1 - q).
        # https://github.com/wrf-model/WRF/blob/v4.8.0/dyn_em/module_initialize_real.F
        flag = _WPS_FIELD_FLAGS.get(name)
        if flag is not None and getattr(ds, flag, 0) != 1:
            raise ValueError(f"Missing WPS {flag}=1 in {path}")
        values = var[:]
        lo, hi = _WPS_FIELD_LIMITS.get(name, (-1e19, 1e19))
        if (
            np.ma.is_masked(values)
            or not lo <= float(np.min(values)) <= float(np.max(values)) <= hi
        ):
            raise ValueError(f"Invalid values in WPS field {name} in {path}")


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
    limit = NC_FLOAT_FILL_THRESHOLD
    if not -limit < np.min(arr, initial=0.0) <= np.max(arr, initial=0.0) < limit:
        valid = (arr > -limit) & (arr < limit)
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
) -> npt.NDArray[np.float32]:
    """Validate WPS or WRF Lambert registration and return the mass-grid longitude slab."""
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
    return xlong


def rotation_sine_needs_flip(
    domain: DomainProfile,
    longitude: npt.NDArray[np.float32],
    cosine: npt.NDArray[np.float32],
    sine: npt.NDArray[np.float32],
    label: str,
) -> bool:
    """Validate both coefficients and identify the legacy unsigned WPS cone."""
    expected_cos, expected_sin = lambert_grid_to_earth_trig(domain, longitude)
    if not np.allclose(cosine, expected_cos, rtol=0.0, atol=1e-5):
        raise ValueError(f"{label}: COSALPHA disagrees with Lambert convergence")
    if np.allclose(sine, expected_sin, rtol=0.0, atol=1e-5):
        return False
    # WPS get_rotang computes an unsigned cone in both hemispheres.
    # https://github.com/wrf-model/WPS/blob/v4.7.0/geogrid/src/process_tile_module.F#L1873-L1895
    if domain.std_parallel_1_deg < 0.0 and np.allclose(sine, -expected_sin, rtol=0.0, atol=1e-5):
        return True
    raise ValueError(f"{label}: SINALPHA disagrees with Lambert convergence")


def check_geogrid_rotation(
    ds: netCDF4.Dataset,
    nc_path: Path,
    domain: DomainProfile,
    xlong: npt.NDArray[np.float32],
    *,
    repair: bool = False,
) -> None:
    """Check WPS coefficients and correct the southern sine on writable geo_em."""
    ny, nx = xlong.shape
    variables = ds.variables
    for suffix, longitude in (
        ("", xlong),
        ("_U", extract_clean_f32_nc(variables, nc_path, "XLONG_U", 0, (ny, nx + 1))),
        ("_V", extract_clean_f32_nc(variables, nc_path, "XLONG_V", 0, (ny + 1, nx))),
    ):
        name = "SINALPHA" + suffix
        sine = extract_clean_f32_nc(variables, nc_path, name, 0, longitude.shape)
        cosine = extract_clean_f32_nc(variables, nc_path, "COSALPHA" + suffix, 0, longitude.shape)
        if rotation_sine_needs_flip(domain, longitude, cosine, sine, str(nc_path)):
            if not repair:
                raise ValueError(
                    f"{nc_path}: incorrect southern Lambert {name}; rerun `av-wrf geog`"
                )
            variables[name][0] = -sine


def verify_wrfout_grid_against_domain(
    ds: object,
    domain: DomainProfile,
    nc_path: Path,
    t_idx: int = 0,
    *,
    science: ScienceProfile | None = None,
) -> npt.NDArray[np.float32]:
    """Validate WRF registration and hybrid eta faces, then return mass-grid longitude."""
    xlong = verify_grid_against_domain(ds, domain, nc_path, t_idx)
    variables: Mapping[str, object] = getattr(ds, "variables", {})
    znw_arr = extract_clean_f32_nc(variables, nc_path, "ZNW", t_idx, (domain.e_vert,))
    validate_solver_znw([float(eta) for eta in znw_arr], domain)
    if science is not None and (
        getattr(ds, "HYBRID_OPT", None) != science.hybrid_opt
        or not math.isclose(float(getattr(ds, "ETAC", math.nan)), science.etac, abs_tol=1e-6)
    ):
        raise ValueError(f"{nc_path}: HYBRID_OPT or ETAC disagrees with ScienceProfile")
    return xlong


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
