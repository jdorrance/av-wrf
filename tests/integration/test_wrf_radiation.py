"""Admission checks for RRTMG surface radiation at saved WRF history times."""

from dataclasses import replace
from pathlib import Path

import netCDF4
import numpy as np
import pytest
from scripts.audit_forecast import _radiation_counts, _read_native

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
        for name in ("SWNORM", "SWDNT", "SWUPT"):
            ds.createVariable(name, "f4", dims)[:] = 0.0
        for name, value in (("SWDNB", 0.0), ("SWUPB", -0.005), ("ALBEDO", -9999.0)):
            ds.variables[name][0, 0, 0] = value
    read_wrfout_step_nc(path, domain, science, require_surface_shortwave=True)
    assert _radiation_counts(_read_native(path, 0))["invalid_daylight_albedo"] == 0

    for name, bad, good, message in (
        ("ALBEDO", -9999.0, 0.3, "daylight ALBEDO"),
        ("SWUPB", -327965.0, 35.0, "negative flux"),
        ("SWUPB", 101.0, 35.0, "SWUPB exceeds SWDNB"),
        ("SWDOWN", -20.0, 620.0, "slope-corrected SWDOWN"),
    ):
        with netCDF4.Dataset(str(path), mode="r+") as ds:
            ds.variables[name][0, 1, 1] = bad
        with pytest.raises(ValueError, match=message):
            read_wrfout_step_nc(path, domain, science, require_surface_shortwave=True)
        counts = _radiation_counts(_read_native(path, 0))
        if name == "ALBEDO":
            assert counts["invalid_daylight_albedo"] == 1
        elif bad < 0:
            assert counts[f"negative_{name}"] == 1
        else:
            assert counts["excess_surface_reflection"] == 1
        with netCDF4.Dataset(str(path), mode="r+") as ds:
            ds.variables[name][0, 1, 1] = good


@pytest.mark.parametrize("name", ("SWDOWN", "SWNORM", "SWDNB", "SWUPB", "SWDNT", "SWUPT"))
def test_native_audit_rejects_nonfinite_flux_at_night(tmp_path: Path, name: str) -> None:
    path = tmp_path / "radiation"
    with netCDF4.Dataset(path, mode="w") as ds:
        for dim in ("Time", "south_north", "west_east"):
            ds.createDimension(dim, 1)
        dims = ("Time", "south_north", "west_east")
        for field in ("ALBEDO", "SWDOWN", "SWNORM", "SWDNB", "SWUPB", "SWDNT", "SWUPT"):
            ds.createVariable(field, "f4", dims)[:] = 0.0
        ds[name][:] = np.nan
    with pytest.raises(ValueError, match=f"Invalid native radiation variable {name}"):
        _read_native(path, 0)


def test_live_mynn_history_requires_cloud_fraction(tmp_path: Path) -> None:
    domain, science = _small_domain_and_science()
    science = replace(science, bl_pbl_physics=5, sf_sfclay_physics=5)
    path = tmp_path / "wrfout"
    _write_real_wrfout(path, domain, "2026-11-12_04:00:00")
    with netCDF4.Dataset(str(path), mode="r+") as ds:
        ds.renameVariable("CLDFRA_BL", "unavailable")
    with pytest.raises(ValueError, match=r"Missing required MYNN history field CLDFRA_BL"):
        read_wrfout_step_nc(path, domain, science, require_mynn_clouds=True)
    for pbl_scheme, coupling in ((1, 1), (5, 0)):
        inactive = replace(science, bl_pbl_physics=pbl_scheme, icloud_bl=coupling)
        step = read_wrfout_step_nc(path, domain, inactive, require_mynn_clouds=True)
        assert step.cldfra_bl is None
    with netCDF4.Dataset(str(path), mode="r+") as ds:
        ds.renameVariable("unavailable", "CLDFRA_BL")
        ds["CLDFRA_BL"][:] = 0.25
    step = read_wrfout_step_nc(path, domain, science, require_mynn_clouds=True)
    assert step.cldfra_bl is not None
    assert step.cldfra_bl.buf[0] == 0.25
