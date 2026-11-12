"""Admission checks for RRTMG surface radiation at saved WRF history times."""

from pathlib import Path

import netCDF4
import pytest

from av_wrf.codecs import read_wrfout_baseline_nc, read_wrfout_step_nc
from tests.integration.test_netcdf_boundary import _small_domain_and_science, _write_real_wrfout


def test_rrtmg_surface_shortwave_admission(tmp_path: Path) -> None:
    domain, science = _small_domain_and_science()
    path = tmp_path / "wrfout"
    _write_real_wrfout(path, domain, "2026-11-12_04:00:00")
    read_wrfout_step_nc(path, domain, science)
    with pytest.raises(ValueError, match=r"shortwave.*missing SWDNB, SWUPB, ALBEDO"):
        read_wrfout_baseline_nc(path, domain, science=science, require_surface_shortwave=True)

    with netCDF4.Dataset(str(path), mode="r+") as ds:
        dims = ("Time", "south_north", "west_east")
        ds.createVariable("SWDNB", "f4", dims)[:] = 100.0
        ds.createVariable("SWUPB", "f4", dims)[:] = 35.0
        ds.createVariable("ALBEDO", "f4", dims, fill_value=-9999.0)[:] = 0.3
        for name, value in (("SWDNB", 0.0), ("SWUPB", -0.005), ("ALBEDO", -9999.0)):
            ds.variables[name][0, 0, 0] = value
    read_wrfout_step_nc(path, domain, science, require_surface_shortwave=True)

    for name, bad, good, message in (
        ("ALBEDO", -9999.0, 0.3, "daylight ALBEDO"),
        ("SWUPB", -327965.0, 35.0, "negative flux"),
        ("SWUPB", 101.0, 35.0, "SWUPB exceeds SWDNB"),
    ):
        with netCDF4.Dataset(str(path), mode="r+") as ds:
            ds.variables[name][0, 1, 1] = bad
        with pytest.raises(ValueError, match=message):
            read_wrfout_step_nc(path, domain, science)
        with netCDF4.Dataset(str(path), mode="r+") as ds:
            ds.variables[name][0, 1, 1] = good
