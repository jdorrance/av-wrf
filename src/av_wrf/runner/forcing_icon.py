"""Plan DWD ICON-EU native input and admit normalized model-level WPS GRIB2."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from itertools import pairwise
from pathlib import Path

from av_wrf.contracts import DomainProfile, ForcingProfile
from av_wrf.core import computational_latlon_bounds
from av_wrf.runner.forcing_recipe import ForcingRecipe
from av_wrf.runner.grib import (
    GRIB_FIELD_KEYS,
    GRIB_KEYS,
    checked_grib_grid,
    grib_number,
    inspect_grib,
)

# DWD model-level inventory and corresponding WPS v4.7 GRIB2 selectors.
# https://opendata.dwd.de/weather/nwp/v1/m/icon-eu/p/
# https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/Variable_Tables/Vtable.ICONm
_LEVELS = range(1, 75)
_AIR = (
    ("P", "PRESSURE", "Pa", (0, 3, 0, 150)),
    ("T", "TT", "K", (0, 0, 0, 150)),
    ("QV", "SPECHUMD", "kg kg-1", (0, 1, 0, 150)),
    ("U", "UU", "m s-1", (0, 2, 2, 150)),
    ("V", "VV", "m s-1", (0, 2, 3, 150)),
)
_HEIGHT = ("HHL", "HGT", "m", (0, 3, 6, 150))
_FIXED = (
    ("FR_LAND", "LANDSEA", "proprtn", (2, 0, 0, 1), 0),
    ("HSURF", "SOILHGT", "m", (0, 3, 6, 1), 0),
)
ICON_INVARIANT_FIELDS = frozenset(("CLAT", "CLON", _HEIGHT[0], *(row[0] for row in _FIXED)))
_SURFACE = (
    ("PS", "PSFC", "Pa", (0, 3, 0, 1), 0),
    ("PMSL", "PMSL", "Pa", (0, 3, 1, 101), 0),
    ("T_G", "SKINTEMP", "K", (0, 0, 0, 1), 0),
    ("T_2M", "TT", "K", (0, 0, 0, 103), 2),
    ("QV_2M", "SPECHUMD", "kg kg-1", (0, 1, 0, 103), 2),
    ("U_10M", "UU", "m s-1", (0, 2, 2, 103), 10),
    ("V_10M", "VV", "m s-1", (0, 2, 3, 103), 10),
    ("W_SNOW", "SNOW", "kg m-2", (0, 1, 60, 1), 0),
    ("H_SNOW", "SNOWH", "m", (0, 1, 11, 1), 0),
    ("H_ICE", "SEAICE", "proprtn", (10, 2, 0, 1), 0),
)
_SOIL_INTERFACES_CM = (0, 1, 3, 9, 27, 81, 243, 729, 2187)
_SOIL_LAYERS = tuple(pairwise(_SOIL_INTERFACES_CM))
_SOIL_CENTERS_CM = tuple((top + bottom) / 2 for top, bottom in _SOIL_LAYERS)
_SOIL_TEMP = (2, 3, 18, 106)
_SOIL_WATER = (2, 3, 20, 106)
ICON_INPUT_SELECTORS = {
    **{field: codes for field, _, _, codes in (*_AIR, _HEIGHT)},
    **{field: codes for field, _, _, codes, _ in (*_FIXED, *_SURFACE)},
    "H_ICE": (10, 2, 1, 1),  # Native thickness, normalized as ice fraction.
    "T_SO": _SOIL_TEMP,
    "W_SO": _SOIL_WATER,
}
# With a missing second surface WPS uses the raw value, including 5 for 0.005 m.
# https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/src/rd_grib2.F#L721-L744
_SOIL_TEMP_SELECTORS = {
    depth / 100: 5 if depth == 0.5 else int(depth) for depth in _SOIL_CENTERS_CM
}
_REQUIRED = frozenset(
    (
        *((*codes, level, level + 1) for _, _, _, codes in (*_AIR, _HEIGHT) for level in _LEVELS),
        *((*codes, level, level) for _, _, _, codes, level in (*_FIXED, *_SURFACE)),
        *((*_SOIL_TEMP, depth / 100, 0) for depth in _SOIL_CENTERS_CM),
        *((*_SOIL_WATER, top / 100, bottom / 100) for top, bottom in _SOIL_LAYERS),
    )
)
_IDENTITY_KEYS = (
    "edition",
    "centre",
    "generatingProcessIdentifier",
    "productDefinitionTemplateNumber",
    "dataDate",
    "dataTime",
    "validityDate",
    "validityTime",
)
_SOIL_SELECTOR_KEYS = (
    "typeOfSecondFixedSurface",
    "scaleFactorOfSecondFixedSurface",
    "scaledValueOfSecondFixedSurface",
    "scaledValueOfFirstFixedSurface",
)


def _icon_inputs(cycle: datetime, hours: tuple[int, ...]) -> dict[str, tuple[str, ...]]:
    """Return ordered DWD v1 object keys for each boundary hour, with invariant keys reused."""
    invariant = (
        *sorted(ICON_INVARIANT_FIELDS - {_HEIGHT[0]}),
        *(f"HHL/lvt1/150/lv1/{level}" for level in (*_LEVELS, _LEVELS.stop)),
    )
    hourly = (
        *(field for field, *_ in _SURFACE),
        *(f"{field}/lvt1/150/lv1/{level}" for field, *_ in _AIR for level in _LEVELS),
        *(f"T_SO/lvt1/106/lv1/{depth / 100}" for depth in _SOIL_CENTERS_CM),
        *(f"W_SO/lvt1/106/lv1/{top / 100}" for top, _ in _SOIL_LAYERS),
    )
    run = f"{cycle:%Y-%m-%dT%H}%3A00"
    fixed = tuple(f"m/icon-eu/p/{field}/r/{run}/s/PT000H00M.grib2" for field in invariant)
    return {
        str(hour): (
            *fixed,
            *(f"m/icon-eu/p/{field}/r/{run}/s/PT{hour:03d}H00M.grib2" for field in hourly),
        )
        for hour in hours
    }


def _vtable() -> str:
    """Render the GRIB2 Vtable from the same selectors used to admit input."""
    rows: list[str] = []
    for field, name, units, codes in (*_AIR, _HEIGHT):
        rows.append(f" | 109 | * | | {name} | {units} | {field} | {' | '.join(map(str, codes))} |")
    for field, name, units, codes, level in (*_FIXED, *_SURFACE):
        level_type = {1: 1, 101: 102, 103: 105}[codes[3]]
        rows.append(
            f" | {level_type} | {level} | | {name} | {units} | {field} | "
            f"{' | '.join(map(str, codes))} |"
        )
    t_code = " | ".join(map(str, _SOIL_TEMP))
    w_code = " | ".join(map(str, _SOIL_WATER))
    for depth, (top, bottom) in zip(_SOIL_CENTERS_CM, _SOIL_LAYERS, strict=True):
        cm = int(depth + 0.5)
        suffix = min(cm, 999)
        selector = _SOIL_TEMP_SELECTORS[depth / 100]
        rows.extend(
            (
                f" | 111 | {selector} | | SOILT{suffix:03d} | K | T_SO | {t_code} |",
                f" | 112 | {top} | {bottom} | SOILM{suffix:03d} | kg m-2 | W_SO | {w_code} |",
            )
        )
    return "\n".join(("---", *rows, "---", ""))


def _check_window(
    source: ForcingProfile, _domain: DomainProfile, cycle: datetime, hours: tuple[int, ...]
) -> None:
    if cycle.hour not in (0, 6, 12, 18):
        raise ValueError(f"No qualified {cycle:%H}Z forcing cycle for {source.profile_id}")
    if hours[-1] > 78:
        raise ValueError(f"ICON-EU hourly cadence ends at f078; cycle requires f{hours[-1]:03d}")
    if not source.use_sh_qv:
        raise ValueError("ICON-EU requires use_sh_qv for native model-level specific humidity")


def _field_key(row: Mapping[str, object], path: Path) -> tuple[object, ...]:
    field = tuple(row.get(key) for key in GRIB_FIELD_KEYS)
    if field[:4] != _SOIL_TEMP:
        return field
    selector = _SOIL_TEMP_SELECTORS.get(grib_number(row, "level"))
    raw = tuple(row.get(key) for key in _SOIL_SELECTOR_KEYS)
    if selector is None or raw != (255, "MISSING", "MISSING", selector):
        raise ValueError(f"Wrong ICON-EU soil-temperature selector in {path}")
    # ecCodes synthesizes bottomLevel for a single depth; GRIB2 has no second surface.
    return (*field[:5], 0)


def _validate_file(
    path: Path, cycle: datetime, source: ForcingProfile, domain: DomainProfile, fhr: int
) -> None:
    valid = cycle + timedelta(hours=fhr)
    expected = (
        2,
        78,
        2,
        0,
        int(cycle.strftime("%Y%m%d")),
        int(cycle.strftime("%H%M")),
        int(valid.strftime("%Y%m%d")),
        int(valid.strftime("%H%M")),
    )
    messages = inspect_grib(
        path,
        GRIB_KEYS
        + ",uvRelativeToGrid:i,bitmapPresent:i,"
        + ",".join(f"{key}:i" for key in _SOIL_SELECTOR_KEYS),
    )
    spacing = source.grid_spacing_deg
    rows = checked_grib_grid(
        path,
        messages,
        computational_latlon_bounds(domain, snap_deg=spacing),
        spacing,
        ("grid_simple",),
    )
    fields: set[tuple[object, ...]] = set()
    for row in rows:
        identity = tuple(row.get(key) for key in _IDENTITY_KEYS)
        if identity != expected:
            raise ValueError(f"Wrong ICON-EU source, cycle or valid time in {path} for f{fhr:03d}")
        if row.get("uvRelativeToGrid") != 0 or row.get("bitmapPresent") != 0:
            raise ValueError(
                f"ICON-EU grid must use earth-relative winds and complete values: {path}"
            )
        field = _field_key(row, path)
        if field in fields or field not in _REQUIRED:
            raise ValueError(f"Duplicate or unexpected ICON-EU field {field} in {path}")
        fields.add(field)
    if missing := _REQUIRED - fields:
        raise ValueError(
            f"Missing {len(missing)} ICON-EU messages in {path}: {sorted(missing)[:4]}"
        )


ICON_EU = ForcingRecipe(
    identity=("DWD", "Vtable.ICONm", 3600, 0.0625),
    check_window=_check_window,
    file_keys=_icon_inputs,
    validate_file=_validate_file,
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
        "SOILHGT",
        "LANDSEA",
        "SOILM",
        "SOILT",
        "SOILT001",
        "SOIL_LEVELS",
        "SNOW",
        "SNOWH",
        "SEAICE",
    ),
    metgrid_soil_dimensions=("num_soilt_levels", "num_soilm_levels"),
    vtable_text=_vtable(),
)

__all__ = ["ICON_EU", "ICON_INPUT_SELECTORS", "ICON_INVARIANT_FIELDS"]
