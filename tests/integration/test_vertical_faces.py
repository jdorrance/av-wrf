"""Native initialization must reject folds that mass-midpoint admission cannot detect."""

from pathlib import Path

import netCDF4
import numpy as np
import pytest

from av_wrf.codecs import validate_wrfinput_nc
from av_wrf.core.vertical import model_column_spacing, validate_column_geometry
from tests.integration.test_netcdf_boundary import _small_domain_and_science, _write_real_wrfout


@pytest.mark.parametrize("face2_m", (50.0, 60.0))
@pytest.mark.parametrize("published", (True, False))
def test_initialized_faces_reject_folds_hidden_by_midpoints(
    tmp_path: Path, face2_m: float, published: bool
) -> None:
    domain, science = _small_domain_and_science()
    path = tmp_path / "wrfinput_d01"
    _write_real_wrfout(path, domain, "2026-11-12_00:00:00")
    validate_wrfinput_nc(path, domain, science, published=published)
    with netCDF4.Dataset(path, "r+") as ds:
        faces = np.asarray((0.0, 60.0, face2_m, 160.0))
        ds["PH"][0] = 0.0
        ds["PHB"][0, :4] = (1200.0 + faces[:, None, None]) * 9.81
        ds["W"][0] = 0.0
        height = np.asarray(ds["PHB"][0, : domain.exported_levels_k + 1], np.float64) / 9.81
    gaps, count = model_column_spacing((height[1:] + height[:-1]) / 2 - 1200.0)
    validate_column_geometry(
        float(gaps.min()), float(gaps.max()), count, domain, gap_limit_m=domain.max_dz_m
    )
    with pytest.raises(ValueError, match="folded geopotential column"):
        validate_wrfinput_nc(path, domain, science, published=published)
