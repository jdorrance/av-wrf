"""Encode regular latitude/longitude fields in the pinned WPS v5 format."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import BinaryIO

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True)
class WpsIntermediateField:
    """One field's WPS metadata; coordinates are degrees, radius is kilometres."""

    valid_time: str
    forecast_hours: float
    source: str
    name: str
    units: str
    description: str
    level: float
    south: float
    west: float
    latitude_step: float
    longitude_step: float
    earth_radius_km: float
    wind_grid_relative: bool


def _text(value: str, size: int) -> bytes:
    raw = value.encode("ascii")
    if not raw or len(raw) > size or any(c < 32 or c > 126 for c in raw):
        raise ValueError(f"Invalid WPS text field: {value!r}")
    return raw.ljust(size, b" ")


def _record(stream: BinaryIO, payload: bytes) -> None:
    size = struct.pack(">i", len(payload))
    stream.write(size)
    stream.write(payload)
    stream.write(size)


def _validate_metadata(field: WpsIntermediateField) -> None:
    values = (
        field.forecast_hours,
        field.level,
        field.south,
        field.west,
        field.latitude_step,
        field.longitude_step,
        field.earth_radius_km,
    )
    if not all(math.isfinite(value) for value in values):
        raise ValueError("WPS field metadata must be finite")


def write_wps_intermediate_field(
    stream: BinaryIO, field: WpsIntermediateField, values: NDArray[np.float64]
) -> None:
    """Write one big-endian v5 slab in south-to-north, west-to-east (y, x) order."""
    if values.ndim != 2 or min(values.shape) < 2 or not np.isfinite(values).all():
        raise ValueError("WPS slab must be a finite two-dimensional grid")
    _validate_metadata(field)
    ny, nx = values.shape
    if (
        not -90 <= field.south <= 90
        or field.latitude_step <= 0
        or field.longitude_step <= 0
        or field.earth_radius_km <= 0
        or field.south + (ny - 1) * field.latitude_step > 90
        or (nx - 1) * field.longitude_step > 360
    ):
        raise ValueError("Invalid WPS latitude/longitude grid")
    # WPS GNU configure uses big-endian, four-byte sequential record markers.
    # https://github.com/wrf-model/WPS/blob/v4.7.0/arch/configure.defaults
    header = struct.pack(
        ">24sf32s9s25s46sfiii",
        _text(field.valid_time, 24),
        field.forecast_hours,
        _text(field.source, 32),
        _text(field.name, 9),
        _text(field.units, 25),
        _text(field.description, 46),
        field.level,
        nx,
        ny,
        0,
    )
    projection = struct.pack(
        ">8s5f",
        b"SWCORNER",
        field.south,
        field.west,
        field.latitude_step,
        field.longitude_step,
        field.earth_radius_km,
    )
    slab = np.asarray(values, dtype=">f4")
    if not np.isfinite(slab).all():
        raise ValueError("WPS slab exceeds float32 range")
    _record(stream, struct.pack(">i", 5))
    _record(stream, header)
    _record(stream, projection)
    # The reader calls this is_wind_grid_rel, so earth-relative winds encode FALSE.
    # https://github.com/wrf-model/WPS/blob/v4.7.0/metgrid/src/read_met_module.F
    _record(stream, struct.pack(">i", int(field.wind_grid_relative)))
    _record(stream, slab.tobytes(order="C"))


__all__ = ["WpsIntermediateField", "write_wps_intermediate_field"]
