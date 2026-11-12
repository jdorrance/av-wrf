"""Validate the NOAA GFS pressure-grid product against the WPS v4.7 GFS table."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path

from av_wrf.contracts import DomainProfile, ForcingProfile
from av_wrf.core import computational_latlon_bounds
from av_wrf.runner.forcing_recipe import ForcingRecipe
from av_wrf.runner.grib import inspect_grib

# GRIB2 codes from the WPS v4.7.0 Vtable.GFS and the NCEP GFS product inventory.
# https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/Variable_Tables/Vtable.GFS
_ATMOSPHERIC = ((0, 0, 0), (0, 1, 1), (0, 2, 2), (0, 2, 3), (0, 3, 5))
_SURFACE = (
    (0, 0, 0, 1, 0, 0),
    (0, 1, 13, 1, 0, 0),
    (0, 3, 0, 1, 0, 0),
    (0, 3, 5, 1, 0, 0),
    (0, 3, 192, 101, 0, 0),
    (2, 0, 0, 1, 0, 0),
    (10, 2, 0, 1, 0, 0),
    (0, 0, 0, 103, 2, 2),
    (0, 1, 1, 103, 2, 2),
    (0, 2, 2, 103, 10, 10),
    (0, 2, 3, 103, 10, 10),
)
_SOIL = tuple(
    (2, 0, param, 106, top, bottom)
    for param in (2, 192)
    for top, bottom in ((0, 0.1), (0.1, 0.4), (0.4, 1), (1, 2))
)
_REQUIRED_SURFACE = frozenset((*_SURFACE, *_SOIL))
_FIELD_KEYS = (
    "discipline",
    "parameterCategory",
    "parameterNumber",
    "typeOfFirstFixedSurface",
    "level",
    "bottomLevel",
)
_GRIB_KEYS = (
    "edition,centre:i,generatingProcessIdentifier:i,productDefinitionTemplateNumber:i,"
    "dataDate:i,dataTime:i,validityDate:i,validityTime:i,"
    "discipline:i,parameterCategory:i,parameterNumber:i,typeOfFirstFixedSurface:i,level:d,"
    "bottomLevel:d,typeOfLevel,gridType,md5Section3,offset:i,totalLength:i,packingType,"
    "latitudeOfFirstGridPointInDegrees:d,latitudeOfLastGridPointInDegrees:d,"
    "longitudeOfFirstGridPointInDegrees:d,longitudeOfLastGridPointInDegrees:d,"
    "iDirectionIncrementInDegrees:d,jDirectionIncrementInDegrees:d,Ni:i,Nj:i"
)


def _number(row: Mapping[str, object], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Missing or invalid GRIB2 key {key}")
    return float(value)


def _validate_gfs_coverage(
    row: Mapping[str, object], domain: DomainProfile, source: ForcingProfile
) -> None:
    w, s, e, n = computational_latlon_bounds(domain, snap_deg=source.grid_spacing_deg)
    dx = _number(row, "iDirectionIncrementInDegrees")
    dy = _number(row, "jDirectionIncrementInDegrees")
    ni, nj = _number(row, "Ni"), _number(row, "Nj")
    lon0 = _number(row, "longitudeOfFirstGridPointInDegrees")
    lon1 = _number(row, "longitudeOfLastGridPointInDegrees")
    south, north = sorted(
        (
            _number(row, "latitudeOfFirstGridPointInDegrees"),
            _number(row, "latitudeOfLastGridPointInDegrees"),
        )
    )
    width = dx * (ni - 1)
    west = w - (w - lon0) % 360.0
    if (
        row.get("gridType") != "regular_ll"
        or not math.isclose(dx, source.grid_spacing_deg, abs_tol=1e-6)
        or not math.isclose(dy, source.grid_spacing_deg, abs_tol=1e-6)
        or min(ni, nj) < 2
        or not math.isclose((lon1 - lon0) % 360.0, width, abs_tol=1e-5)
        or not math.isclose(north - south, dy * (nj - 1), abs_tol=1e-5)
        or (ni * dx < 360.0 - 1e-5 and e > west + width + 1e-5)
        or s < south - 1e-5
        or n > north + 1e-5
    ):
        raise ValueError("GFS grid geometry or coverage disagrees with the forcing cutout")


def _validate_gfs_inventory(
    fields: set[tuple[object, ...]], domain: DomainProfile, source: ForcingProfile
) -> None:
    levels = [{row[4] for row in fields if row[:4] == (*var, 100)} for var in _ATMOSPHERIC]
    pressure = sorted(float(x) for x in levels[0] if isinstance(x, (int, float)))
    # The configured minimum counts the surface level that metgrid adds.
    if (
        len(pressure) + 1 < source.metgrid_levels
        or any(group != levels[0] for group in levels[1:])
        or not pressure
        or pressure[0] > domain.p_top_pa
        or pressure[-1] < 100_000.0
    ):
        raise ValueError("Incomplete or inconsistent GFS isobaric field inventory")
    missing = _REQUIRED_SURFACE - fields
    if missing:
        raise ValueError(f"Missing GFS surface/soil messages: {sorted(missing)}")


def _grib_field(row: Mapping[str, object]) -> tuple[object, ...] | None:
    field = tuple(row.get(k) for k in _FIELD_KEYS)
    if field[:4] == (0, 0, 0, 106):
        # Vtable.GFS maps both encodings to the same soil-temperature layer.
        field = (2, 0, 2, 106, *field[4:])
    if field[3] != 100:
        return field if field in _REQUIRED_SURFACE else None
    if field[:3] not in _ATMOSPHERIC:
        return None
    unit = row.get("typeOfLevel")
    if unit not in ("isobaricInPa", "isobaricInhPa"):
        raise ValueError("Unknown GRIB isobaric coordinate units")
    pressure_pa = _number(row, "level") * (100.0 if unit == "isobaricInhPa" else 1.0)
    # ungrib's default pmin is 100 Pa. metgrid adds one surface level.
    # https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/src/read_namelist.F
    return (*field[:4], pressure_pa, pressure_pa) if pressure_pa >= 100.0 else None


def _validate_file(
    path: Path, cycle: datetime, source: ForcingProfile, domain: DomainProfile, fhr: int
) -> None:
    """Check GRIB2 identity, regional coverage, and the WPS field inventory."""
    valid = cycle + timedelta(hours=fhr)
    expected = (
        2,
        7,
        int(cycle.strftime("%Y%m%d")),
        int(cycle.strftime("%H%M")),
        int(valid.strftime("%Y%m%d")),
        int(valid.strftime("%H%M")),
    )
    messages = inspect_grib(path, _GRIB_KEYS)
    _validate_gfs_coverage(messages[0], domain, source)
    grid = messages[0].get("md5Section3")
    offset = 0
    fields: set[tuple[object, ...]] = set()
    for row in messages:
        identity = tuple(
            row.get(k)
            for k in ("edition", "centre", "dataDate", "dataTime", "validityDate", "validityTime")
        )
        if identity != expected or row.get("generatingProcessIdentifier") != (96 if fhr else 81):
            raise ValueError(f"Wrong GFS source, cycle or valid time in {path} for f{fhr:03d}")
        if not isinstance(grid, str) or not grid or row.get("md5Section3") != grid:
            raise ValueError(f"Mixed or missing GFS grids in {path}")
        length = int(_number(row, "totalLength"))
        if row.get("offset") != offset or length < 20 or row.get("packingType") == "grid_ccsds":
            raise ValueError(f"Invalid GRIB2 framing or unsupported packing in {path}")
        offset += length
        if row.get("productDefinitionTemplateNumber") == 0:
            field = _grib_field(row)
            if field is not None:
                if field in fields:
                    raise ValueError(f"Duplicate required GFS field {field} in {path}")
                fields.add(field)
    if offset != path.stat().st_size:
        raise ValueError(f"Incomplete GRIB2 framing in {path}")
    _validate_gfs_inventory(fields, domain, source)


def _check_window(
    source: ForcingProfile, _domain: DomainProfile, cycle: datetime, hours: tuple[int, ...]
) -> None:
    # https://www.nco.ncep.noaa.gov/pmb/products/gfs/nomads/
    if cycle.hour not in (0, 6, 12, 18):
        raise ValueError(f"No {cycle:%H}Z forcing cycle for {source.profile_id}")
    if hours[-1] > 120:
        raise ValueError(
            f"{source.profile_id} cadence ends at f120; cycle requires f{hours[-1]:03d}"
        )


def _file_keys(cycle: datetime, hours: tuple[int, ...]) -> Mapping[str, str]:
    return {
        str(hour): f"gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos/gfs.t{cycle:%H}z.pgrb2.0p25.f{hour:03d}"
        for hour in hours
    }


GFS = ForcingRecipe(
    identity=("NOAA", "Vtable.GFS", 3600, 0.25),
    check_window=_check_window,
    file_keys=_file_keys,
    validate_file=_validate_file,
    metgrid_fields=(
        "PRES",
        "GHT",
        "TT",
        "RH",
        "UU",
        "VV",
        "PSFC",
        "PMSL",
        "SKINTEMP",
        "SOILHGT",
        "LANDSEA",
        "SM",
        "ST",
        "SNOW",
        "SEAICE",
    ),
    metgrid_soil_dimensions=("num_st_layers", "num_sm_layers"),
)

__all__ = ["GFS"]
