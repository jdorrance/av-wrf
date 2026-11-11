"""Typed local NetCDF4 reader boundary for raw WRF history frames."""

from __future__ import annotations

from pathlib import Path

from av_wrf.core import RawWrfStepInput


def verify_local_wrf_nc(nc_path: Path) -> str:
    """Verify local wrfout/wrfinput file exists before NetCDF4 handle open in Phase 2."""
    if not nc_path.is_file():
        raise FileNotFoundError(f"Missing local WRF NetCDF file: {nc_path}")
    return nc_path.name


__all__ = ["RawWrfStepInput", "verify_local_wrf_nc"]
