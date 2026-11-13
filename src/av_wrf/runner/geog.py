"""Nested static-geography domain profiles and deterministic geo_em tar bundles."""

from __future__ import annotations

import re
import shutil
import tarfile
from math import floor
from numbers import Integral
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

from av_wrf.codecs import check_geogrid_rotation, verify_grid_against_domain, verify_wps_fields
from av_wrf.contracts import DomainProfile
from av_wrf.core import (
    computational_grid_origin_xy_m,
    forcing_domain,
    latlon_to_lambert_xy_m,
)
from av_wrf.runner.bundle import LoadedBundle

if TYPE_CHECKING:
    import netCDF4


def _ocean_cells(domain: DomainProfile, water: npt.NDArray[np.bool_]) -> npt.NDArray[np.bool_]:
    selected = np.zeros(water.shape, dtype=np.bool_)
    ny, nx = water.shape
    x0, y0 = computational_grid_origin_xy_m(domain)
    pending: list[tuple[int, int]] = []
    for lat, lon in domain.ocean_seeds_lat_lon_deg:
        x, y = latlon_to_lambert_xy_m(
            lat,
            lon,
            origin_lat_deg=domain.center_lat_deg,
            central_meridian_deg=domain.central_meridian_deg,
            std_parallel_1_deg=domain.std_parallel_1_deg,
            std_parallel_2_deg=domain.std_parallel_2_deg,
            radius_m=domain.earth_radius_m,
        )
        i, j = floor((x - x0) / domain.dx_m + 0.5), floor((y - y0) / domain.dy_m + 0.5)
        # A parent may contain the seed while its child ends inland; never snap to an edge.
        if not (0 <= j < ny and 0 <= i < nx):
            continue
        if not water[j, i]:
            raise ValueError(f"Ocean seed {(lat, lon)} maps to land at {(j, i)}")
        pending.append((j, i))
    while pending:
        j, i = pending.pop()
        if selected[j, i]:
            continue
        selected[j, i] = True
        # Four-neighbor connectivity preserves separate lakes touching a domain boundary.
        for jj, ii in ((j - 1, i), (j + 1, i), (j, i - 1), (j, i + 1)):
            if 0 <= jj < ny and 0 <= ii < nx and water[jj, ii] and not selected[jj, ii]:
                pending.append((jj, ii))
    return selected


def _normalize_ocean_water(ds: netCDF4.Dataset, domain: DomainProfile, *, repair: bool) -> None:
    categories = tuple(getattr(ds, key, None) for key in ("ISWATER", "ISLAKE", "NUM_LAND_CAT"))
    indices = tuple(int(value) for value in categories if isinstance(value, Integral))
    if len(indices) != 3:
        raise ValueError("Ocean correction requires integer ISWATER, ISLAKE and NUM_LAND_CAT")
    ocean, lake, count = indices
    if ocean == lake or not (1 <= ocean <= count and 1 <= lake <= count):
        raise ValueError("Ocean correction requires distinct valid water and lake categories")
    shape = (domain.e_sn - 1, domain.e_we - 1)
    if "LANDUSEF" not in ds.variables or ds["LANDUSEF"].shape != (1, count, *shape):
        raise ValueError("Ocean correction requires LANDUSEF on the native category grid")
    raw = ds["LANDUSEF"][0]
    fractions = np.asarray(raw)
    # WPS stores normalized fractions as float32, leaving ulp-scale summation residuals.
    if (
        np.ma.is_masked(raw)
        or not np.all(np.isfinite(fractions) & (fractions >= 0) & (fractions <= 1))
        or not np.allclose(fractions.sum(axis=0, dtype=np.float64), 1, rtol=0, atol=1e-6)
    ):
        raise ValueError("Invalid normalized LANDUSEF category fractions")
    landuse = np.asarray(ds["LU_INDEX"][0])
    mask = np.asarray(ds["LANDMASK"][0])
    water = (landuse == ocean) | (landuse == lake)
    if landuse.shape != shape or mask.shape != shape or not np.array_equal(mask, ~water):
        raise ValueError("Ocean correction requires consistent LANDMASK and water categories")
    selected = _ocean_cells(domain, water)
    fractions = fractions[[ocean - 1, lake - 1]]
    if not np.any(selected & ((landuse == lake) | (fractions[1] != 0))):
        return
    if not repair:
        raise ValueError("Configured ocean waterbody still contains lake category or fraction")
    # MODIS labels some seas as lakes; real.exe derives LAKEMASK from LU_INDEX, ignoring
    # lake_min_elev for explicit lake categories (WRF v4.8 module_initialize_real.F).
    landuse[selected] = ocean
    # An admitted float32 sum can exceed one by an ulp; preserve valid fraction bounds.
    fractions[0, selected] = np.minimum(1, fractions[0, selected] + fractions[1, selected])
    fractions[1, selected] = 0
    ds["LU_INDEX"][0] = landuse
    ds["LANDUSEF"][0, ocean - 1] = fractions[0]
    ds["LANDUSEF"][0, lake - 1] = fractions[1]


def geog_domain_profiles(bundle: LoadedBundle) -> tuple[tuple[str, DomainProfile], ...]:
    """Return `(tag, profile)` for every active geogrid domain, coarse parent first."""
    dom = bundle.domain
    outer = forcing_domain(dom, bundle.primary_source.grid_spacing_deg)
    return (("d01", dom),) if outer is dom else (("d01", outer), ("d02", dom))


def validate_lake_initialization(geog_path: Path, wrfinput_path: Path) -> None:
    """Check real.exe preserves lake selection and initializes finite water temperatures."""
    import netCDF4

    with netCDF4.Dataset(str(geog_path)) as geo, netCDF4.Dataset(str(wrfinput_path)) as wrf:
        landuse = np.asarray(geo["LU_INDEX"][0])
        expected = landuse == geo.ISLAKE
        verify_wps_fields(wrf, wrfinput_path, ("LAKEMASK", "SST", "TSK"))
        # real.exe changes lake LU_INDEX to ISWATER while preserving the separate LAKEMASK.
        # https://github.com/wrf-model/WRF/blob/v4.8.0/dyn_em/module_initialize_real.F
        if not np.array_equal(wrf["LAKEMASK"][0], expected):
            raise ValueError(f"{wrfinput_path}: LAKEMASK differs from sealed geography")
        ocean = landuse == geo.ISWATER
        for name in ("SST", "TSK"):
            temperature = np.asarray(wrf[name][0])[ocean]
            # Match real.exe's broad water-temperature sanity range (same upstream source).
            if np.any((temperature <= 150) | (temperature >= 400)):
                raise ValueError(f"{wrfinput_path}: ocean {name} outside (150, 400) K")


def validate_geo_em(
    path: Path, domain: DomainProfile, *, lake_depth: bool, repair: bool = False
) -> None:
    """Admit static WPS fields; normalize rotation and configured sea categories before sealing."""
    import netCDF4

    with netCDF4.Dataset(str(path), "r+" if repair else "r") as ds:
        xlong = verify_grid_against_domain(ds, domain, path, wps=True)
        fields = ("HGT_M", "LU_INDEX", "LANDMASK")
        verify_wps_fields(ds, path, (*fields, "LAKE_DEPTH") if lake_depth else fields)
        check_geogrid_rotation(ds, path, domain, xlong, repair=repair)
        if domain.ocean_seeds_lat_lon_deg:
            _normalize_ocean_water(ds, domain, repair=repair)


def pack_geog_files(paths: tuple[Path, ...], dest: Path) -> Path:
    """Pack geo_em files into a deterministic POSIX tar (sorted, zeroed metadata)."""
    with tarfile.open(dest, "w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(paths, key=lambda p: p.name):
            info = tar.gettarinfo(str(path), arcname=path.name)
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
            with path.open("rb") as stream:
                tar.addfile(info, stream)
    return dest


def extract_geog_bundle(bundle_path: Path, dest_dir: Path) -> tuple[Path, ...]:
    """Extract a nested geo_em tar and return the member paths in dNN order."""
    with tarfile.open(bundle_path, "r") as tar:
        members = tar.getmembers()
        names = {m.name for m in members}
        if (
            len(members) != 2
            or names != {"geo_em.d01.nc", "geo_em.d02.nc"}
            or any(not member.isfile() for member in members)
        ):
            raise ValueError(f"Unexpected nested geography bundle members: {sorted(names)}")
        for member in members:
            # Exact regular-file names and exclusive creation prevent archive aliases/overwrites.
            stream = tar.extractfile(member)
            if stream is None:
                raise ValueError(f"Missing geography payload: {member.name}")
            with stream, (dest_dir / member.name).open("xb") as target:
                shutil.copyfileobj(stream, target)
    return tuple(dest_dir / name for name in sorted(names))


def verify_geog_sources(log: str, domain: DomainProfile) -> None:
    """Admit configured field-specific geography sources from the WPS log."""
    selected = set(re.findall(r"Using (\S+) data source for (\S+)\.", log))
    required = {(domain.lai_source, "LAI12M"), (domain.varsso_source, "VAR_SSO")}
    for configured, name in ((domain.dem_source, "HGT_M"), (domain.landuse_source, "LANDUSEF")):
        # GEOGRID tries the global token list independently for each field; 30s may resolve
        # a `default` field before the fallback token (WPS/source_data_module.F).
        allowed = {configured} if configured != "default" else {"30s", "default"}
        if not any((source, name) in selected for source in allowed):
            raise ValueError(f"Geogrid selected {sorted(selected)}, expected {configured}/{name}")
    if not required <= selected:
        raise ValueError(f"Geogrid selected {sorted(selected)}, expected {sorted(required)}")


__all__ = [
    "extract_geog_bundle",
    "geog_domain_profiles",
    "pack_geog_files",
    "validate_geo_em",
    "validate_lake_initialization",
    "verify_geog_sources",
]
