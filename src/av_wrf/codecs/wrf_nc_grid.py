"""WRF NetCDF `Times` indexing, multi-hour gust window slicing, and Lambert/ZNW verification."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from av_wrf.contracts import DomainProfile, format_iso_utc, parse_iso_utc
from av_wrf.core import lambert_xy_m_to_latlon, validate_solver_znw


def verify_local_wrf_nc(nc_path: Path) -> str:
    """Verify local wrfout/wrfinput file exists before NetCDF4 handle open."""
    if not nc_path.is_file():
        raise FileNotFoundError(f"Missing local WRF NetCDF file: {nc_path}")
    return nc_path.name


def parse_wrf_timestamp(raw_chars: str) -> tuple[str, int]:
    """Parse WRF `YYYY-MM-DD_HH:MM:SS` character stamp into canonical ISO-8601 UTC + epoch."""
    dt = parse_iso_utc(raw_chars.strip().replace("_", "T") + "Z")
    return format_iso_utc(dt), int(dt.timestamp())


def slice_nc_var(variables: Mapping[str, object], name: str, time_index: int) -> object:
    """Index time slice `time_index` from NetCDF4 variable mapping."""
    getter = getattr(variables[name], "__getitem__", None)
    if getter is None:
        raise TypeError(f"NetCDF variable '{name}' is not indexable")
    return getter(time_index)


def _index_wrfout_times(wrfout_dir: Path) -> dict[str, tuple[Path, int, int]]:
    import netCDF4

    index: dict[str, tuple[Path, int, int]] = {}
    for nc_path in sorted(wrfout_dir.glob("wrfout_d01_*")):
        verify_local_wrf_nc(nc_path)
        with netCDF4.Dataset(str(nc_path), mode="r") as ds:
            if "Times" not in ds.variables:
                raise ValueError(f"Missing 'Times' variable in {nc_path}")
            times_var = ds.variables["Times"]
            for t_idx in range(int(times_var.shape[0])):
                raw_s = bytes(times_var[t_idx].tobytes()).decode("ascii")
                iso_utc, unix_s = parse_wrf_timestamp(raw_s)
                if iso_utc in index:
                    raise ValueError(f"Duplicate wrfout timestamp {iso_utc} in {nc_path}")
                index[iso_utc] = (nc_path, t_idx, unix_s)
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
    f0_p, f0_i, _ = index[publish_origin_utc]
    return (f0_p, f0_i), tuple((index[vt][0], index[vt][1]) for vt in valid_times_utc)


def select_cycle_wrfout_windows(
    wrfout_dir: Path, publish_origin_utc: str, valid_times_utc: tuple[str, ...]
) -> tuple[tuple[Path, int], tuple[tuple[tuple[Path, int], ...], ...]]:
    """Return `(f000, per_lead_window_slices)` capturing all history dumps in `(t_prev, t_curr]`."""
    index = _index_wrfout_times(wrfout_dir)
    if publish_origin_utc not in index:
        raise ValueError(f"Missing f000 wrfout frame for publish_origin_utc {publish_origin_utc}")
    missing = [vt for vt in valid_times_utc if vt not in index]
    if missing:
        raise ValueError(f"Missing wrfout lead timestamps: {', '.join(missing[:6])}")
    ordered = sorted(index.values(), key=lambda item: item[2])
    f0_p, f0_i, prev_s = index[publish_origin_utc]
    windows: list[tuple[tuple[Path, int], ...]] = []
    for vt in valid_times_utc:
        end_p, end_i, end_s = index[vt]
        in_win = tuple((p, idx) for p, idx, ts in ordered if prev_s < ts <= end_s)
        windows.append(in_win if in_win else ((end_p, end_i),))
        prev_s = end_s
    return (f0_p, f0_i), tuple(windows)


def verify_wrfout_grid_against_domain(
    ds: object, domain: DomainProfile, nc_path: Path, t_idx: int = 0
) -> None:
    """Verify wrfout Lambert global attributes, XLAT/XLONG, and ZNW against DomainProfile."""
    import numpy as np

    for attr_name, expected, tol in (
        ("MAP_PROJ", 1.0, 0.0),
        ("DX", domain.dx_m, 1e-2),
        ("DY", domain.dy_m, 1e-2),
        ("TRUELAT1", domain.std_parallel_1_deg, 1e-3),
        ("TRUELAT2", domain.std_parallel_2_deg, 1e-3),
        ("STAND_LON", domain.central_meridian_deg, 1e-3),
    ):
        if not hasattr(ds, attr_name):
            raise ValueError(f"Missing global attribute '{attr_name}' in {nc_path}")
        actual = float(getattr(ds, attr_name))
        if abs(actual - expected) > tol:
            raise ValueError(f"{nc_path}: {attr_name}={actual} != configured {expected}")
    variables: Mapping[str, object] = getattr(ds, "variables", {})
    if "ZNW" in variables:
        znw_arr = np.asarray(slice_nc_var(variables, "ZNW", t_idx), dtype=np.float64)
        validate_solver_znw([float(x) for x in znw_arr.ravel()], domain)
    if "XLAT" not in variables or "XLONG" not in variables:
        raise ValueError(f"Missing XLAT/XLONG grid coordinates in {nc_path}")
    ny, nx = domain.e_sn - 1, domain.e_we - 1
    xlat = np.asarray(slice_nc_var(variables, "XLAT", t_idx), dtype=np.float64)
    xlong = np.asarray(slice_nc_var(variables, "XLONG", t_idx), dtype=np.float64)
    if xlat.shape != (ny, nx) or xlong.shape != (ny, nx):
        raise ValueError(f"{nc_path}: XLAT/XLONG shape != {(ny, nx)}")
    for jr, ir in ((0, 0), (ny // 2, nx // 2), (ny - 1, nx - 1)):
        exp_lat, exp_lon = lambert_xy_m_to_latlon(
            (ir - 0.5 * (nx - 1)) * domain.dx_m,
            (jr - 0.5 * (ny - 1)) * domain.dy_m,
            origin_lat_deg=domain.center_lat_deg,
            central_meridian_deg=domain.central_meridian_deg,
            std_parallel_1_deg=domain.std_parallel_1_deg,
            std_parallel_2_deg=domain.std_parallel_2_deg,
            radius_m=domain.earth_radius_m,
        )
        dlon = ((float(xlong[jr, ir]) - exp_lon + 180.0) % 360.0) - 180.0
        if abs(float(xlat[jr, ir]) - exp_lat) > 0.02 or abs(dlon) > 0.02:
            raise ValueError(f"{nc_path}: XLAT/XLONG at ({jr},{ir}) disagrees with DomainProfile")
