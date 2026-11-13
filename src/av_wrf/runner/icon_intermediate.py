"""Convert normalized ICON GRIB2 messages to WPS v5 intermediate fields."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from math import isclose
from pathlib import Path
from typing import BinaryIO

import numpy as np

from av_wrf.codecs import (
    IconGribCutout,
    WpsIntermediateField,
    read_wps_intermediate_fields,
    write_wps_intermediate_field,
)
from av_wrf.contracts import DomainProfile, ForcingProfile
from av_wrf.core import computational_latlon_bounds, forcing_domain
from av_wrf.runner.forcing_icon import (
    _AIR,
    _FIXED,
    _HEIGHT,
    _LEVELS,
    _SOIL_CENTERS_CM,
    _SOIL_LAYERS,
    _SOIL_TEMP,
    _SOIL_TEMP_SELECTORS,
    _SOIL_WATER,
    _SURFACE,
)

type _Selector = tuple[int, int, int, int]
type _Row = tuple[str, str, str, int | None, int | None]


def _vtable_rows(vtable: str) -> dict[_Selector, list[_Row]]:
    rows: dict[_Selector, list[_Row]] = {}
    for line in vtable.splitlines():
        if not line.startswith(" |"):
            continue
        parts = [part.strip() for part in line.split("|")]
        selector = tuple(int(value) for value in parts[7:11])
        if len(selector) != 4:
            raise ValueError("Invalid ICON Vtable selector")
        key: _Selector = (selector[0], selector[1], selector[2], selector[3])
        top = int(parts[2]) if parts[2] not in ("", "*") else None
        bottom = int(parts[3]) if parts[3] else None
        rows.setdefault(key, []).append((parts[4], parts[5], parts[6], top, bottom))
    return rows


def _select(
    selector: _Selector,
    level: float,
    bottom_level: float,
    raw_top: int,
    rows: dict[_Selector, list[_Row]],
) -> tuple[str, str, str, float, float]:
    candidates = rows.get(selector, [])
    if selector[3] == 106:
        # WPS uses the raw first-surface value for a single-depth GRIB2 field.
        # https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/src/rd_grib2.F
        selector_top = raw_top if selector[2] == 18 else round(level * 100)
        selector_bottom = round(bottom_level * 100)
        candidates = [
            r
            for r in candidates
            if r[3] == selector_top and (r[4] is None or r[4] == selector_bottom)
        ]
    elif selector[3] != 150:
        candidates = [r for r in candidates if r[3] == level]
    if len(candidates) != 1:
        raise ValueError(f"Unknown ICON normalized field {selector} at level {level}")
    name, units, description, top, bottom = candidates[0]
    factor = 1.0
    if selector[3] == 106 and bottom is not None and top is not None:
        # kg/m2 divided by water density and thickness yields m3/m3.
        factor = 1.0 / (1000.0 * (bottom - top) / 100.0)
        units = "m3 m-3"
    # WPS rd_grib2 uses hybrid indices directly and special surface/MSL levels.
    wps_level = level if selector[3] == 150 else 201300.0 if selector[3] == 101 else 200100.0
    return name, units, description, wps_level, factor


def _field(gid: int, rows: dict[_Selector, list[_Row]]) -> tuple[str, str, str, float, float]:
    import eccodes as ec

    selector: _Selector = (
        int(ec.codes_get_long(gid, "discipline")),
        int(ec.codes_get_long(gid, "parameterCategory")),
        int(ec.codes_get_long(gid, "parameterNumber")),
        int(ec.codes_get_long(gid, "typeOfFirstFixedSurface")),
    )
    return _select(
        selector,
        float(ec.codes_get_double(gid, "level")),
        float(ec.codes_get_double(gid, "bottomLevel")),
        int(ec.codes_get_long(gid, "scaledValueOfFirstFixedSurface")),
        rows,
    )


def _inventory_rows() -> dict[_Selector, list[_Row]]:
    rows: dict[_Selector, list[_Row]] = {}
    for field, name, units, codes in (*_AIR, _HEIGHT):
        rows.setdefault(codes, []).append((name, units, field, None, None))
    for field, name, units, codes, level in (*_FIXED, *_SURFACE):
        rows.setdefault(codes, []).append((name, units, field, level, None))
    for depth, (top, bottom) in zip(_SOIL_CENTERS_CM, _SOIL_LAYERS, strict=True):
        suffix = min(int(depth + 0.5), 999)
        rows.setdefault(_SOIL_TEMP, []).append(
            (f"SOILT{suffix:03d}", "K", "T_SO", _SOIL_TEMP_SELECTORS[depth / 100], None)
        )
        rows.setdefault(_SOIL_WATER, []).append(
            (f"SOILM{suffix:03d}", "kg m-2", "W_SO", top, bottom)
        )
    return rows


def write_icon_fields(
    cutout: IconGribCutout,
    fields: Mapping[str, Sequence[str]],
    destination: BinaryIO,
    cycle: datetime,
    hour: int,
    top_pa: float,
) -> None:
    """Write remapped native ICON slabs directly to WPS v5 intermediate format."""
    rows = _inventory_rows()
    valid = (cycle + timedelta(hours=hour)).strftime("%Y-%m-%d_%H:%M:%S")
    for slab in cutout.fields(fields, hour, top_pa):
        name, units, description, level, factor = _select(
            slab.selector, slab.level, slab.bottom, slab.raw_top, rows
        )
        grid = slab.grid
        field = WpsIntermediateField(
            valid,
            float(hour),
            "DWD",
            name,
            units,
            description,
            level,
            grid.south,
            grid.west % 360.0,
            grid.spacing,
            grid.spacing,
            slab.radius_km,
            False,
        )
        write_wps_intermediate_field(destination, field, slab.values * factor)


def write_icon_intermediate(
    normalized: BinaryIO, destination: BinaryIO, cycle: datetime, hour: int, vtable: str
) -> None:
    """Decode normalized ICON GRIB and write one boundary time in WPS v5 format."""
    import eccodes as ec

    rows = _vtable_rows(vtable)
    valid = cycle + timedelta(hours=hour)
    while header := normalized.read(16):
        if len(header) != 16 or header[:4] != b"GRIB" or header[7] != 2:
            raise ValueError("Malformed normalized ICON GRIB header")
        size = int.from_bytes(header[8:16], "big")
        if size < 20:
            raise ValueError("Malformed normalized ICON GRIB length")
        body = normalized.read(size - 16)
        if len(body) != size - 16 or not body.endswith(b"7777"):
            raise ValueError("Truncated normalized ICON GRIB message")
        raw = header + body
        gid = ec.codes_new_from_message(raw)
        try:
            name, units, description, level, factor = _field(gid, rows)
            nx = int(ec.codes_get_long(gid, "Ni"))
            ny = int(ec.codes_get_long(gid, "Nj"))
            field = WpsIntermediateField(
                valid.strftime("%Y-%m-%d_%H:%M:%S"),
                float(hour),
                "DWD",
                name,
                units,
                description,
                level,
                float(ec.codes_get_double(gid, "latitudeOfFirstGridPointInDegrees")),
                float(ec.codes_get_double(gid, "longitudeOfFirstGridPointInDegrees")),
                float(ec.codes_get_double(gid, "jDirectionIncrementInDegrees")),
                float(ec.codes_get_double(gid, "iDirectionIncrementInDegrees")),
                float(ec.codes_get_double(gid, "radius")) / 1000.0,
                False,
            )
            values = np.asarray(ec.codes_get_values(gid), dtype=np.float64).reshape(ny, nx)
            write_wps_intermediate_field(destination, field, values * factor)
        finally:
            ec.codes_release(gid)


def _expected(vtable: str) -> dict[tuple[str, float], str]:
    expected: dict[tuple[str, float], str] = {}
    for selector, rows in _vtable_rows(vtable).items():
        levels = _LEVELS if selector[3] == 150 else (201300 if selector[3] == 101 else 200100,)
        for name, units, _, _, bottom in rows:
            if selector[3] == 106 and bottom is not None:
                units = "m3 m-3"
            for level in levels:
                expected[name, float(level)] = units
    return expected


def _check_grid(
    field: WpsIntermediateField,
    shape: tuple[int, int],
    bounds: tuple[float, float, float, float],
    spacing: float,
) -> None:
    west, south, east, north = bounds
    grid_west = west - (west - field.west) % 360.0
    ny, nx = shape
    if (
        not isclose(field.latitude_step, spacing, abs_tol=1e-6)
        or not isclose(field.longitude_step, spacing, abs_tol=1e-6)
        or east > grid_west + (nx - 1) * field.longitude_step + 1e-5
        or south < field.south - 1e-5
        or north > field.south + (ny - 1) * field.latitude_step + 1e-5
    ):
        raise ValueError("ICON WPS grid does not cover the forcing domain")


def validate_icon_intermediate(
    path: Path,
    cycle: datetime,
    source: ForcingProfile,
    domain: DomainProfile,
    hour: int,
    vtable: str,
) -> None:
    """Validate all ICON WPS slabs, field units, cycle, common grid and domain coverage."""
    expected = _expected(vtable)
    valid = (cycle + timedelta(hours=hour)).strftime("%Y-%m-%d_%H:%M:%S")
    bounds = computational_latlon_bounds(
        forcing_domain(domain, source.grid_spacing_deg), snap_deg=source.grid_spacing_deg
    )
    grid: tuple[float, float, float, float, float, tuple[int, int]] | None = None
    for slab in read_wps_intermediate_fields(path, expected_time=valid, required_fields=expected):
        field = slab.field
        key = field.name, field.level
        if (
            expected.get(key) != field.units
            or field.source != "DWD"
            or field.forecast_hours != hour
        ):
            raise ValueError(f"Unexpected ICON WPS field metadata: {key}")
        shape = slab.values.shape
        current = (
            field.south,
            field.west,
            field.latitude_step,
            field.longitude_step,
            field.earth_radius_km,
            shape,
        )
        if grid is None:
            _check_grid(field, shape, bounds, source.grid_spacing_deg)
            grid = current
        elif current != grid:
            raise ValueError("ICON WPS fields have different grids")


__all__ = ["validate_icon_intermediate", "write_icon_fields", "write_icon_intermediate"]
