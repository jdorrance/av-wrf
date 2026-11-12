"""Read GRIB message metadata using the solver image's ecCodes tools."""

import json
import math
import subprocess
from collections.abc import Iterator, Mapping, Sequence
from contextlib import suppress
from pathlib import Path

GRIB_FIELD_KEYS = (
    "discipline",
    "parameterCategory",
    "parameterNumber",
    "typeOfFirstFixedSurface",
    "level",
    "bottomLevel",
)
GRIB_KEYS = (
    "edition,centre:i,generatingProcessIdentifier:i,productDefinitionTemplateNumber:i,"
    "dataDate:i,dataTime:i,validityDate:i,validityTime:i,"
    "discipline:i,parameterCategory:i,parameterNumber:i,typeOfFirstFixedSurface:i,level:d,"
    "bottomLevel:d,typeOfLevel,gridType,md5Section3,offset:i,totalLength:i,packingType,"
    "latitudeOfFirstGridPointInDegrees:d,latitudeOfLastGridPointInDegrees:d,"
    "longitudeOfFirstGridPointInDegrees:d,longitudeOfLastGridPointInDegrees:d,"
    "iDirectionIncrementInDegrees:d,jDirectionIncrementInDegrees:d,Ni:i,Nj:i,"
    "iScansNegatively:i,jScansPositively:i,jPointsAreConsecutive:i,alternativeRowScanning:i,"
    "numberOfDataPoints:i,min:d,max:d,average:d"
)


def inspect_grib(path: Path, keys: str) -> list[dict[str, object]]:
    """Read requested ecCodes keys per message, preserving file order."""
    try:
        proc = subprocess.run(
            ["grib_ls", "-j", "-p", keys, str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Install the ecCodes grib_ls tool to verify forcing") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"ecCodes rejected {path}: {exc.stderr.strip()}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ecCodes timed out reading {path}") from exc
    doc = json.loads(proc.stdout)
    messages = doc.get("messages") if isinstance(doc, dict) else None
    if (
        not messages
        or not isinstance(messages, list)
        or any(not isinstance(row, dict) for row in messages)
    ):
        raise ValueError(f"No readable GRIB2 messages in {path}")
    return messages


def grib_number(row: Mapping[str, object], key: str) -> float:
    """Read a finite numeric ecCodes key."""
    value = row.get(key)
    # Older grib_ls JSON quotes doubles printed in scientific notation.
    if isinstance(value, str):
        with suppress(ValueError):
            value = float(value)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Missing or invalid GRIB2 key {key}")
    return float(value)


def _validate_latlon_coverage(
    row: Mapping[str, object], bounds: tuple[float, float, float, float], spacing: float
) -> None:
    """Require a regular grid to span the unwrapped west, south, east, north cutout."""
    w, s, e, n = bounds
    dx = grib_number(row, "iDirectionIncrementInDegrees")
    dy = grib_number(row, "jDirectionIncrementInDegrees")
    ni, nj = grib_number(row, "Ni"), grib_number(row, "Nj")
    lon0 = grib_number(row, "longitudeOfFirstGridPointInDegrees")
    lon1 = grib_number(row, "longitudeOfLastGridPointInDegrees")
    lat0 = grib_number(row, "latitudeOfFirstGridPointInDegrees")
    lat1 = grib_number(row, "latitudeOfLastGridPointInDegrees")
    south, north = sorted((lat0, lat1))
    scan_north = row.get("jScansPositively")
    width = dx * (ni - 1)
    west = w - (w - lon0) % 360.0
    if (
        row.get("gridType") != "regular_ll"
        # WPS reads the sign of the latitude scan and assumes eastward contiguous rows.
        # https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/src/rd_grib2.F#L391-L413
        or any(
            row.get(key) != 0
            for key in ("iScansNegatively", "jPointsAreConsecutive", "alternativeRowScanning")
        )
        or not math.isclose(dx, spacing, abs_tol=1e-6)
        or not math.isclose(dy, spacing, abs_tol=1e-6)
        or min(ni, nj) < 2
        or not math.isclose((lon1 - lon0) % 360.0, width, abs_tol=1e-5)
        or scan_north not in (0, 1)
        or not math.isclose(lat1 - lat0, (1 if scan_north else -1) * dy * (nj - 1), abs_tol=1e-5)
        or (ni * dx < 360.0 - 1e-5 and e > west + width + 1e-5)
        or s < south - 1e-5
        or n > north + 1e-5
    ):
        raise ValueError("GRIB2 grid geometry or coverage disagrees with the forcing cutout")


def checked_grib_grid(
    path: Path,
    rows: Sequence[dict[str, object]],
    bounds: tuple[float, float, float, float],
    spacing: float,
    packings: tuple[str, ...],
) -> Iterator[dict[str, object]]:
    """Yield messages on one covered grid with contiguous, WPS-readable framing."""
    _validate_latlon_coverage(rows[0], bounds, spacing)
    grid = rows[0].get("md5Section3")
    points = grib_number(rows[0], "Ni") * grib_number(rows[0], "Nj")
    offset = 0
    for row in rows:
        if not isinstance(grid, str) or not grid or row.get("md5Section3") != grid:
            raise ValueError(f"Mixed or missing GRIB2 grids in {path}")
        length = int(grib_number(row, "totalLength"))
        if row.get("offset") != offset or length < 20 or row.get("packingType") not in packings:
            raise ValueError(f"Invalid GRIB2 framing or packing in {path}")
        if row.get("numberOfDataPoints") != points:
            raise ValueError(f"Invalid GRIB2 data-point count in {path}")
        for key in ("min", "max", "average"):
            grib_number(row, key)
        offset += length
        yield row
    if offset != path.stat().st_size:
        raise ValueError(f"Incomplete GRIB2 framing in {path}")


__all__ = [
    "GRIB_FIELD_KEYS",
    "GRIB_KEYS",
    "checked_grib_grid",
    "grib_number",
    "inspect_grib",
]
