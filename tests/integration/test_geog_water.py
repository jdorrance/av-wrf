"""Seeded ocean repair preserves geography and rejects uncorrected lake masks."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import netCDF4
import numpy as np
import numpy.typing as npt
import pytest

from av_wrf.contracts import DomainProfile
from av_wrf.core import computational_grid_origin_xy_m, lambert_xy_m_to_latlon
from av_wrf.runner.geog import validate_geo_em, validate_lake_initialization
from tests.integration.test_netcdf_boundary import _small_domain_and_science

_SEA = ((0, 1), (1, 1), (1, 2), (2, 2), (2, 3), (3, 3))
_LAKES = ((4, 4), (5, 1), (5, 2), (5, 8), (6, 8))
_FIELDS = ("LU_INDEX", "LANDUSEF", "LANDMASK", "HGT_M", "LAKE_DEPTH")


def _point(domain: DomainProfile, j: float, i: float) -> tuple[float, float]:
    x0, y0 = computational_grid_origin_xy_m(domain)
    return lambert_xy_m_to_latlon(
        x0 + i * domain.dx_m,
        y0 + j * domain.dy_m,
        origin_lat_deg=domain.center_lat_deg,
        central_meridian_deg=domain.central_meridian_deg,
        std_parallel_1_deg=domain.std_parallel_1_deg,
        std_parallel_2_deg=domain.std_parallel_2_deg,
        radius_m=domain.earth_radius_m,
    )


def _write_geography(path: Path, domain: DomainProfile) -> None:
    ny, nx = domain.e_sn - 1, domain.e_we - 1
    categories = np.ones((ny, nx), dtype=np.float32)
    fractions = np.zeros((21, ny, nx), dtype=np.float32)
    fractions[0] = 1.0
    for j, i in (*_SEA, *_LAKES):
        categories[j, i] = 21
        fractions[0, j, i], fractions[16, j, i], fractions[20, j, i] = 0.25, 0.15, 0.6
    # An existing ocean cell must connect lake-labelled parts of the same sea.
    categories[1, 2] = 17
    fractions[16, 1, 2], fractions[20, 1, 2] = 0.6, 0.15
    terrain = np.full((ny, nx), 1200.0, dtype=np.float32)
    for j, i in _SEA:
        terrain[j, i] = 0.0
    terrain[3, 3] = 117.0
    terrain[4, 4] = 0.0
    latlon = np.array([[_point(domain, j, i) for i in range(nx)] for j in range(ny)])
    with netCDF4.Dataset(path, "w") as ds:
        for name, size in (("Time", 1), ("south_north", ny), ("west_east", nx), ("land_cat", 21)):
            ds.createDimension(name, size)
        ds.MAP_PROJ, ds.DX, ds.DY = 1, domain.dx_m, domain.dy_m
        ds.CEN_LAT, ds.CEN_LON = domain.center_lat_deg, domain.center_lon_deg
        ds.TRUELAT1, ds.TRUELAT2 = domain.std_parallel_1_deg, domain.std_parallel_2_deg
        ds.STAND_LON = domain.central_meridian_deg
        ds.ISWATER, ds.ISLAKE, ds.NUM_LAND_CAT, ds.FLAG_LAKE_DEPTH = 17, 21, 21, 1
        ds.MMINLU = "MODIFIED_IGBP_MODIS_NOAH"
        dims = ("Time", "south_north", "west_east")
        for name, data in (
            ("XLAT_M", latlon[:, :, 0]),
            ("XLONG_M", latlon[:, :, 1]),
            ("LU_INDEX", categories),
            ("LANDMASK", (categories == 1).astype(np.float32)),
            ("HGT_M", terrain),
            ("LAKE_DEPTH", np.full((ny, nx), 25.5, dtype=np.float32)),
        ):
            ds.createVariable(name, "f4", dims)[:] = data
        ds.createVariable("LANDUSEF", "f4", ("Time", "land_cat", *dims[1:]))[:] = fractions


def _read_fields(path: Path) -> dict[str, npt.NDArray[np.float32]]:
    with netCDF4.Dataset(path) as ds:
        return {name: np.asarray(ds[name][0], dtype=np.float32) for name in _FIELDS}


def _write_wrfinput(geog_path: Path) -> Path:
    landuse = _read_fields(geog_path)["LU_INDEX"]
    path = geog_path.with_name("wrfinput_d01")
    with netCDF4.Dataset(path, "w") as ds:
        for name, size in (
            ("Time", 1),
            ("south_north", landuse.shape[0]),
            ("west_east", landuse.shape[1]),
        ):
            ds.createDimension(name, size)
        dims = ("Time", "south_north", "west_east")
        ds.createVariable("LAKEMASK", "i4", dims)[:] = landuse == 21
        for name in ("SST", "TSK"):
            ds.createVariable(name, "f4", dims)[:] = 285.0
    return path


@pytest.fixture
def geography(tmp_path: Path) -> Iterator[tuple[Path, DomainProfile]]:
    domain, _ = _small_domain_and_science()
    domain = replace(domain, e_we=10, e_sn=8, bdy_trim_cells=1)
    domain = replace(domain, ocean_seeds_lat_lon_deg=(_point(domain, 1.2, 1.1),))
    path = tmp_path / "geo_em.d01.nc"
    _write_geography(path, domain)
    # Registration remains real; staggered wind rotation has independent boundary tests.
    with patch("av_wrf.runner.geog.check_geogrid_rotation"):
        yield path, domain


def test_repair_follows_seeded_water_and_preserves_other_geography(
    geography: tuple[Path, DomainProfile],
) -> None:
    path, domain = geography
    before = _read_fields(path)
    with pytest.raises(ValueError):
        validate_geo_em(path, domain, lake_depth=True)
    validate_geo_em(path, domain, lake_depth=True, repair=True)
    after = _read_fields(path)
    expected_lu, expected_fraction = before["LU_INDEX"].copy(), before["LANDUSEF"].copy()
    for j, i in _SEA:
        expected_lu[j, i] = 17
        expected_fraction[16, j, i] += expected_fraction[20, j, i]
        expected_fraction[20, j, i] = 0.0
    np.testing.assert_array_equal(after["LU_INDEX"], expected_lu)
    np.testing.assert_array_equal(after["LANDUSEF"], expected_fraction)
    for name in ("LANDMASK", "HGT_M", "LAKE_DEPTH"):
        np.testing.assert_array_equal(after[name], before[name])
    np.testing.assert_array_equal(after["LANDUSEF"].sum(axis=0), 1.0)
    sealed = path.read_bytes()
    validate_geo_em(path, domain, lake_depth=True)
    validate_geo_em(path, domain, lake_depth=True, repair=True)
    assert path.read_bytes() == sealed


@pytest.mark.parametrize("tamper", ("category", "fraction"))
def test_admission_rejects_remaining_selected_lake_category_or_fraction(
    geography: tuple[Path, DomainProfile], tamper: str
) -> None:
    path, domain = geography
    validate_geo_em(path, domain, lake_depth=True, repair=True)
    with netCDF4.Dataset(path, "r+") as ds:
        if tamper == "category":
            ds["LU_INDEX"][0, 3, 3] = 21
        else:
            ds["LANDUSEF"][0, 16, 3, 3] -= 0.125
            ds["LANDUSEF"][0, 20, 3, 3] = 0.125
    original = path.read_bytes()
    with pytest.raises(ValueError):
        validate_geo_em(path, domain, lake_depth=True)
    assert path.read_bytes() == original


def test_fraction_roundoff_remains_admissible_after_repair(
    geography: tuple[Path, DomainProfile],
) -> None:
    path, domain = geography
    with netCDF4.Dataset(path, "r+") as ds:
        ds["LANDUSEF"][0, :, 1, 2] = 0.0
        # Both float32 inputs are valid; their merged value rounds one ulp above one.
        ds["LANDUSEF"][0, 16, 1, 2] = np.float32(0.8)
        ds["LANDUSEF"][0, 20, 1, 2] = np.float32(0.20000008)
    validate_geo_em(path, domain, lake_depth=True, repair=True)
    after = _read_fields(path)
    assert after["LANDUSEF"][16, 1, 2] == 1.0
    assert after["LANDUSEF"][20, 1, 2] == 0.0
    sealed = path.read_bytes()
    validate_geo_em(path, domain, lake_depth=True)
    validate_geo_em(path, domain, lake_depth=True, repair=True)
    assert path.read_bytes() == sealed


def test_land_seed_fails_without_snapping_to_adjacent_water(
    geography: tuple[Path, DomainProfile],
) -> None:
    path, domain = geography
    domain = replace(domain, ocean_seeds_lat_lon_deg=(_point(domain, 2, 1),))
    original = path.read_bytes()
    with pytest.raises(ValueError):
        validate_geo_em(path, domain, lake_depth=True, repair=True)
    assert path.read_bytes() == original


@pytest.mark.parametrize("outside", (False, True))
def test_absent_or_outside_seeds_preserve_all_lakes(
    geography: tuple[Path, DomainProfile], outside: bool
) -> None:
    path, domain = geography
    seeds = (_point(domain, -2, 1),) if outside else ()
    domain = replace(domain, ocean_seeds_lat_lon_deg=seeds)
    original = path.read_bytes()
    validate_geo_em(path, domain, lake_depth=True)
    validate_geo_em(path, domain, lake_depth=True, repair=True)
    assert path.read_bytes() == original


def test_outside_seed_does_not_skip_later_inside_seed(
    geography: tuple[Path, DomainProfile],
) -> None:
    path, domain = geography
    domain = replace(
        domain,
        ocean_seeds_lat_lon_deg=(_point(domain, -2, 1), *domain.ocean_seeds_lat_lon_deg),
    )
    validate_geo_em(path, domain, lake_depth=True, repair=True)
    after = _read_fields(path)
    assert all(after["LU_INDEX"][j, i] == 17 for j, i in _SEA)
    assert all(after["LU_INDEX"][j, i] == 21 for j, i in _LAKES)


def test_two_inside_seeds_select_distinct_waterbodies(
    geography: tuple[Path, DomainProfile],
) -> None:
    path, domain = geography
    domain = replace(
        domain,
        ocean_seeds_lat_lon_deg=(*domain.ocean_seeds_lat_lon_deg, _point(domain, 5, 8)),
    )
    validate_geo_em(path, domain, lake_depth=True, repair=True)
    after = _read_fields(path)
    assert all(after["LU_INDEX"][j, i] == 17 for j, i in (*_SEA, (5, 8), (6, 8)))
    assert all(after["LU_INDEX"][j, i] == 21 for j, i in ((4, 4), (5, 1), (5, 2)))
    validate_geo_em(path, domain, lake_depth=True)


@pytest.mark.parametrize(
    ("name", "value"),
    (
        ("ISLAKE", None),
        ("ISWATER", 0),
        ("ISLAKE", 22),
        ("ISLAKE", 17),
        ("ISLAKE", 20.5),
        ("NUM_LAND_CAT", 20),
    ),
)
def test_repair_rejects_malformed_water_category_metadata(
    geography: tuple[Path, DomainProfile], name: str, value: float | None
) -> None:
    path, domain = geography
    with netCDF4.Dataset(path, "r+") as ds:
        if value is None:
            ds.delncattr(name)
        else:
            ds.setncattr(name, value)
    original = path.read_bytes()
    with pytest.raises(ValueError):
        validate_geo_em(path, domain, lake_depth=True, repair=True)
    assert path.read_bytes() == original


@pytest.mark.parametrize("tamper", ("masked", "nonfinite", "negative", "sum"))
def test_repair_rejects_invalid_fractions_even_in_unselected_lake(
    geography: tuple[Path, DomainProfile], tamper: str
) -> None:
    path, domain = geography
    with netCDF4.Dataset(path, "r+") as ds:
        fractions = ds["LANDUSEF"]
        if tamper == "masked":
            fractions[0, 20, 5, 8] = np.ma.masked
        elif tamper == "nonfinite":
            fractions[0, 20, 5, 8] = np.nan
        elif tamper == "negative":
            fractions[0, 16, 5, 8], fractions[0, 20, 5, 8] = -0.125, 0.875
        else:
            fractions[0, 0, 5, 8] = 0.5
    original = path.read_bytes()
    with pytest.raises(ValueError):
        validate_geo_em(path, domain, lake_depth=True, repair=True)
    assert path.read_bytes() == original


@pytest.mark.parametrize("tamper", ("mask", "zero_sst", "zero_tsk", "nan", "masked"))
def test_real_initialization_requires_sealed_lake_mask_and_finite_water_temperatures(
    geography: tuple[Path, DomainProfile], tamper: str
) -> None:
    geog_path, domain = geography
    validate_geo_em(geog_path, domain, lake_depth=True, repair=True)
    wrfinput_path = _write_wrfinput(geog_path)
    validate_lake_initialization(geog_path, wrfinput_path)
    with netCDF4.Dataset(wrfinput_path, "r+") as ds:
        if tamper == "mask":
            ds["LAKEMASK"][0, 1, 1] = 1
        elif tamper.startswith("zero_"):
            ds[tamper.removeprefix("zero_").upper()][0, 1, 1] = 0.0
        elif tamper == "nan":
            ds["TSK"][0, 5, 8] = np.nan
        else:
            ds["SST"][0, 5, 8] = np.ma.masked
    with pytest.raises(ValueError):
        validate_lake_initialization(geog_path, wrfinput_path)


@pytest.mark.parametrize("field", ("SST", "TSK"))
@pytest.mark.parametrize("temperature_k", (1.0, 150.0, 400.0, 9999.0))
def test_initialization_rejects_unphysical_ocean_temperature_and_bounds(
    geography: tuple[Path, DomainProfile], field: str, temperature_k: float
) -> None:
    geog_path, domain = geography
    validate_geo_em(geog_path, domain, lake_depth=True, repair=True)
    wrfinput_path = _write_wrfinput(geog_path)
    with netCDF4.Dataset(wrfinput_path, "r+") as ds:
        ds[field][0, 1, 1] = temperature_k
    with pytest.raises(ValueError):
        validate_lake_initialization(geog_path, wrfinput_path)
