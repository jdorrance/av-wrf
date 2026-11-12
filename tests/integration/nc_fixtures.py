"""Shared NetCDF fixture writer for 3D wrfout variables."""

from __future__ import annotations

import netCDF4
import numpy as np

from av_wrf.codecs import synthetic_thm_from_dry_theta
from av_wrf.contracts import DomainProfile
from av_wrf.core import estimate_auto_levels_agl_m


def populate_3d_nc_vars(
    ds: netCDF4.Dataset, domain: DomainProfile, ny: int, nx: int, nz: int
) -> None:
    mids = estimate_auto_levels_agl_m(domain, terrain_m=1200.0, etac=0.2)
    faces_agl = [0.0]
    for m in mids:
        faces_agl.append(faces_agl[-1] + 2.0 * (m - faces_agl[-1]))
    pb = np.empty((1, nz, ny, nx), dtype=np.float32)
    phb = np.empty((1, nz + 1, ny, nx), dtype=np.float32)
    for k in range(nz):
        pb[0, k] = np.float32(87_800.0 * (0.97**k))
    for kw in range(nz + 1):
        phb[0, kw] = np.float32((1200.0 + faces_agl[kw]) * 9.81)
    thm_v = synthetic_thm_from_dry_theta(8.0, 0.005)
    for nm, val in (
        ("P", 0.0),
        ("T", 8.0),
        ("QVAPOR", 0.005),
        ("THM", thm_v),
        ("CLDFRA", 0.1),
        ("CLDFRA_BL", 0.0),
    ):
        ds.createVariable(nm, "f4", ("Time", "bottom_top", "south_north", "west_east"))[:] = val
    ds.createVariable("PB", "f4", ("Time", "bottom_top", "south_north", "west_east"))[:] = pb
    ds.createVariable("PH", "f4", ("Time", "bottom_top_stag", "south_north", "west_east"))[:] = 0.0
    ds.createVariable("PHB", "f4", ("Time", "bottom_top_stag", "south_north", "west_east"))[:] = phb
    ds.createVariable("W", "f4", ("Time", "bottom_top_stag", "south_north", "west_east"))[:] = 0.1
    ds.createVariable("U", "f4", ("Time", "bottom_top", "south_north", "west_east_stag"))[:] = 5.0
    ds.createVariable("V", "f4", ("Time", "bottom_top", "south_north_stag", "west_east"))[:] = 2.0
