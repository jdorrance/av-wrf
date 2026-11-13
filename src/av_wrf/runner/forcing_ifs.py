"""ECMWF IFS pressure-level inventory and WPS intermediate admission."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from av_wrf.contracts import DomainProfile, ForcingProfile
from av_wrf.core import computational_latlon_bounds, forcing_domain
from av_wrf.runner.forcing_recipe import ForcingRecipe

LEVELS = (10, 50, 100, 150, 200, 250, 300, 400, 500, 600, 700, 850, 925, 1000)
AIR = {
    "t": ("TT", "K"),
    "u": ("UU", "m s-1"),
    "v": ("VV", "m s-1"),
    "q": ("SPECHUMD", "kg kg-1"),
    "gh": ("GHT", "m"),
}
SURFACE = {
    "sp": ("PSFC", "Pa"),
    "msl": ("PMSL", "Pa"),
    "skt": ("SKINTEMP", "K"),
    "lsm": ("LANDSEA", "proprtn"),
    "sd": ("SNOW", "kg m-2"),
    "rsn": ("SNOWH", "m"),
    "z": ("SOILHGT", "m"),
    "2t": ("TT", "K"),
    "2d": ("SPECHUMD", "kg kg-1"),
    "10u": ("UU", "m s-1"),
    "10v": ("VV", "m s-1"),
}
SURFACE_HEIGHTS_M = {"2t": 2, "2d": 2, "10u": 10, "10v": 10}
SOURCE_UNITS = {
    **dict.fromkeys(("t", "skt", "2t", "2d", "sot"), "K"),
    **dict.fromkeys(("u", "v", "10u", "10v"), "m s**-1"),
    **dict.fromkeys(("sp", "msl"), "Pa"),
    "q": "kg kg**-1",
    "gh": "gpm",
    "lsm": "(0 - 1)",
    "sd": "m of water equivalent",
    "rsn": "kg m**-3",
    "z": "m**2 s**-2",
    "vsw": "m**3 m**-3",
}
SOIL_SUFFIXES = ("000007", "007028", "028100", "100289")
REQUIRED = (
    frozenset((name, float(level * 100)) for name, _ in AIR.values() for level in LEVELS)
    | frozenset(
        (name, 201300.0 if param == "msl" else 200100.0) for param, (name, _) in SURFACE.items()
    )
    | frozenset(
        (f"{prefix}{suffix}", 200100.0) for prefix in ("ST", "SM") for suffix in SOIL_SUFFIXES
    )
)
UNITS = {name: unit for name, unit in (*AIR.values(), *SURFACE.values())} | {
    **{f"ST{s}": "K" for s in SOIL_SUFFIXES},
    **{f"SM{s}": "m3 m-3" for s in SOIL_SUFFIXES},
}


def _file_keys(cycle: datetime, hours: tuple[int, ...]) -> dict[str, tuple[str, ...]]:
    prefix = f"{cycle:%Y%m%d}/{cycle:%H}z/ifs/0p1/oper/{cycle:%Y%m%d%H}0000"
    return {
        str(hour): tuple(
            dict.fromkeys(
                (
                    f"{prefix}-{hour}h-oper-fc-pl.grib2",
                    f"{prefix}-{hour}h-oper-fc-sfc.grib2",
                    f"{prefix}-0h-oper-fc-sfc.grib2",
                )
            )
        )
        for hour in hours
    }


def _check_window(
    source: ForcingProfile, domain: DomainProfile, cycle: datetime, hours: tuple[int, ...]
) -> None:
    if cycle.hour not in (0, 6, 12, 18) or not hours or any(h < 0 or h % 3 for h in hours):
        raise ValueError("IFS requires a synoptic cycle and three-hourly forecast steps")
    if hours[-1] > (144 if cycle.hour in (0, 12) else 90):
        raise ValueError("IFS requested forecast exceeds the available three-hourly horizon")
    expected = (len(LEVELS) + 1, len(SOIL_SUFFIXES))
    if not source.use_sh_qv or (source.metgrid_levels, source.metgrid_soil_levels) != expected:
        raise ValueError(
            f"IFS requires specific humidity, {expected[0]} metgrid levels "
            f"and {expected[1]} soil layers"
        )
    if domain.p_top_pa < min(LEVELS) * 100:
        raise ValueError("IFS pressure levels do not reach the WRF model top")


def validate_ifs_file(
    path: Path, cycle: datetime, source: ForcingProfile, domain: DomainProfile, fhr: int
) -> None:
    """Admit the complete, consistently gridded IFS intermediate for one valid time."""
    from av_wrf.codecs import read_wps_intermediate_fields

    bounds = computational_latlon_bounds(
        forcing_domain(domain, source.grid_spacing_deg), snap_deg=source.grid_spacing_deg
    )
    expected_time = (cycle + timedelta(hours=fhr)).strftime("%Y-%m-%d_%H:%M:%S")
    grid: tuple[float, float, float, float, float, tuple[int, int]] | None = None
    for slab in read_wps_intermediate_fields(
        path, expected_time=expected_time, required_fields=REQUIRED
    ):
        field = slab.field
        ny, nx = slab.values.shape
        actual_grid = (
            field.south,
            field.west,
            field.latitude_step,
            field.longitude_step,
            field.earth_radius_km,
            slab.values.shape,
        )
        if grid is None:
            grid = actual_grid
        west, south, east, north = bounds
        # Align the float32 west edge before comparing it to the planned float64 bounds.
        grid_west = west + (field.west - west + 180) % 360 - 180
        if (
            (field.name, field.level) not in REQUIRED
            or field.units != UNITS.get(field.name)
            or actual_grid != grid
            or abs(field.earth_radius_km - domain.earth_radius_m / 1000) > 0.01
            or field.source != "ECMWF IFS"
            or field.forecast_hours != fhr
            or abs(field.latitude_step - source.grid_spacing_deg) > 1e-5
            or abs(field.longitude_step - source.grid_spacing_deg) > 1e-5
            or south < field.south - 1e-5
            or north > field.south + (ny - 1) * field.latitude_step + 1e-5
            or west < grid_west - 1e-5
            or east > grid_west + (nx - 1) * field.longitude_step + 1e-5
        ):
            raise ValueError(f"Invalid IFS intermediate metadata or coverage in {path}")
        if field.name == "LANDSEA" and not ((slab.values == 0) | (slab.values == 1)).all():
            raise ValueError(f"IFS intermediate land mask must contain only 0 and 1: {path}")


IFS = ForcingRecipe(
    identity=("ECMWF", "Vtable.ECMWF", 10800, 0.1),
    check_window=_check_window,
    file_keys=_file_keys,
    validate_file=validate_ifs_file,
    metgrid_fields=(
        "PRES",
        "GHT",
        "TT",
        "SPECHUMD",
        "UU",
        "VV",
        "PSFC",
        "PMSL",
        "SKINTEMP",
        "LANDSEA",
        "SOILHGT",
        "ST",
        "SM",
        "SNOW",
        "SNOWH",
    ),
    metgrid_soil_dimensions=("num_st_layers", "num_sm_layers"),
)

__all__ = ["IFS", "validate_ifs_file"]
