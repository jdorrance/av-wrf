"""Stream and validate regular latitude/longitude WPS v5 intermediate fields."""

from __future__ import annotations

import math
import struct
from collections.abc import Collection, Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import BinaryIO

import numpy as np
from numpy.typing import NDArray

from av_wrf.codecs.wps_intermediate import WpsIntermediateField

_HEADER = struct.Struct(">24sf32s9s25s46sfiii")
_PROJECTION = struct.Struct(">8s5f")
_MAX_SLAB_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class WpsIntermediateSlab:
    """Validated metadata and finite native float32 values in (y, x) order."""

    field: WpsIntermediateField
    values: NDArray[np.float32]


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    parts = bytearray()
    while len(parts) < size:
        chunk = stream.read(size - len(parts))
        if not chunk:
            raise ValueError("Truncated WPS record")
        parts.extend(chunk)
    return bytes(parts)


def _read_record(stream: BinaryIO, size: int, *, eof: bool = False) -> bytes | None:
    marker = stream.read(1)
    if not marker and eof:
        return None
    marker += _read_exact(stream, 3)
    length = struct.unpack(">i", marker)[0]
    if length != size or length < 0 or length > _MAX_SLAB_BYTES:
        raise ValueError(f"Invalid WPS record length: {length}, expected {size}")
    payload = _read_exact(stream, length)
    if _read_exact(stream, 4) != marker:
        raise ValueError("Mismatched WPS record markers")
    return payload


def _required_record(stream: BinaryIO, size: int) -> bytes:
    record = _read_record(stream, size)
    assert record is not None
    return record


def _decode_text(raw: bytes) -> str:
    if any(byte < 32 or byte > 126 for byte in raw):
        raise ValueError("Invalid WPS text")
    text = raw.decode("ascii").rstrip(" ")
    if not text:
        raise ValueError("Empty WPS text")
    return text


def _validate_time(value: str) -> None:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d_%H:%M:%S")
    except ValueError as exc:
        raise ValueError(f"Invalid WPS timestamp: {value!r}") from exc
    if len(value) != 19 or parsed.strftime("%Y-%m-%d_%H:%M:%S") != value:
        raise ValueError(f"Invalid WPS timestamp: {value!r}")


def _read_field(stream: BinaryIO) -> WpsIntermediateSlab | None:
    version = _read_record(stream, 4, eof=True)
    if version is None:
        return None
    if struct.unpack(">i", version)[0] != 5:
        raise ValueError("Unsupported WPS intermediate version")
    time, hours, source, name, units, desc, level, nx, ny, proj = _HEADER.unpack(
        _required_record(stream, _HEADER.size)
    )
    if proj != 0 or nx < 2 or ny < 2 or nx * ny * 4 > _MAX_SLAB_BYTES:
        raise ValueError("Invalid WPS projection or dimensions")
    corner, south, west, dy, dx, radius = _PROJECTION.unpack(
        _required_record(stream, _PROJECTION.size)
    )
    if corner != b"SWCORNER":
        raise ValueError("Invalid WPS grid origin")
    if not all(math.isfinite(v) for v in (hours, level, south, west, dy, dx, radius)):
        raise ValueError("Non-finite WPS metadata")
    if (
        not -90 <= south <= 90
        or dy <= 0
        or dx <= 0
        or radius <= 0
        or south + (ny - 1) * dy > 90.0001
        or (nx - 1) * dx > 360.0001
    ):
        raise ValueError("Invalid WPS grid")
    # WPS reads this logical as is_wind_grid_rel; zero denotes earth-relative winds.
    # https://github.com/wrf-model/WPS/blob/v4.7.0/metgrid/src/read_met_module.F
    if struct.unpack(">i", _required_record(stream, 4))[0] != 0:
        raise ValueError("Invalid WPS wind flag: earth-relative required")
    field = WpsIntermediateField(
        _decode_text(time),
        hours,
        _decode_text(source),
        _decode_text(name),
        _decode_text(units),
        _decode_text(desc),
        level,
        south,
        west,
        dy,
        dx,
        radius,
        False,
    )
    _validate_time(field.valid_time)
    values = np.frombuffer(_required_record(stream, nx * ny * 4), dtype=">f4")
    values = values.astype(np.float32)
    if not np.isfinite(values).all():
        raise ValueError("Non-finite WPS slab")
    return WpsIntermediateSlab(field, values.reshape(ny, nx))


def _read_fields(
    stream: BinaryIO, expected_time: str | None, required_fields: Collection[tuple[str, float]]
) -> Iterator[WpsIntermediateSlab]:
    if expected_time is not None:
        _validate_time(expected_time)
    seen: set[tuple[str, float]] = set()
    while (slab := _read_field(stream)) is not None:
        field = slab.field
        if expected_time is None:
            expected_time = field.valid_time
        if field.valid_time != expected_time:
            raise ValueError("WPS timestamp mismatch")
        key = (field.name, field.level)
        if key in seen:
            raise ValueError(f"Duplicate WPS field: {key}")
        seen.add(key)
        yield slab
    if not seen:
        raise ValueError("Empty WPS intermediate file")
    missing = set(required_fields) - seen
    if missing:
        raise ValueError(f"Missing required WPS fields: {sorted(missing)}")


def read_wps_intermediate_fields(
    path_or_stream: str | Path | BinaryIO,
    *,
    expected_time: str | None = None,
    required_fields: Collection[tuple[str, float]] = (),
) -> Iterator[WpsIntermediateSlab]:
    """Read v5 slabs; exhaust the iterator to validate the required (name, level) inventory.

    Times use WPS UTC YYYY-MM-DD_HH:MM:SS. Level values use WPS units (Pa for
    pressure). Malformed records, unexpected times and missing inventory raise ValueError.
    Caller-owned binary streams remain open; path inputs are closed on iterator completion.
    """
    if isinstance(path_or_stream, (str, Path)):
        with open(path_or_stream, "rb") as stream:
            yield from _read_fields(stream, expected_time, required_fields)
    else:
        yield from _read_fields(path_or_stream, expected_time, required_fields)


__all__ = ["WpsIntermediateSlab", "read_wps_intermediate_fields"]
