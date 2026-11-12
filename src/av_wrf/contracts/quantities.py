"""Nominal physical quantity, reference-frame, and contiguous float32/int32 slab types."""

from __future__ import annotations

from array import array
from collections.abc import Iterable
from dataclasses import dataclass
from typing import NewType


@dataclass(frozen=True)
class Slab2DF32:
    """Contiguous C-order float32 2D buffer (ny, nx) compatible with NumPy/Zarr."""

    ny: int
    nx: int
    buf: array[float]

    def __post_init__(self) -> None:
        if self.ny < 1 or self.nx < 1 or self.buf.typecode != "f":
            raise ValueError("Slab2DF32 requires positive (ny, nx) and 'f' float32 array")
        if len(self.buf) != self.ny * self.nx:
            raise ValueError(f"Buffer length {len(self.buf)} != {self.ny} * {self.nx}")

    @staticmethod
    def from_values(ny: int, nx: int, values: Iterable[float]) -> Slab2DF32:
        return Slab2DF32(ny=ny, nx=nx, buf=array("f", values))

    def at(self, j: int, i: int) -> float:
        return float(self.buf[j * self.nx + i])


@dataclass(frozen=True)
class Slab2DI32:
    """Contiguous C-order int32 2D buffer (ny, nx) for WRF accumulator bucket counters."""

    ny: int
    nx: int
    buf: array[int]

    def __post_init__(self) -> None:
        if self.ny < 1 or self.nx < 1 or self.buf.typecode != "i" or self.buf.itemsize != 4:
            raise ValueError("Slab2DI32 requires positive (ny, nx) and 4-byte 'i' int32 array")
        if len(self.buf) != self.ny * self.nx:
            raise ValueError(f"Int buffer length {len(self.buf)} != {self.ny} * {self.nx}")

    @staticmethod
    def from_values(ny: int, nx: int, values: Iterable[int]) -> Slab2DI32:
        return Slab2DI32(ny=ny, nx=nx, buf=array("i", values))

    def at(self, j: int, i: int) -> int:
        return int(self.buf[j * self.nx + i])


@dataclass(frozen=True)
class Slab3DF32:
    """Contiguous C-order float32 3D buffer (nz, ny, nx) compatible with NumPy/Zarr."""

    nz: int
    ny: int
    nx: int
    buf: array[float]

    def __post_init__(self) -> None:
        if self.nz < 1 or self.ny < 1 or self.nx < 1 or self.buf.typecode != "f":
            raise ValueError("Slab3DF32 requires positive (nz, ny, nx) and 'f' array")
        if len(self.buf) != self.nz * self.ny * self.nx:
            raise ValueError("Buffer length does not match nz * ny * nx")

    @staticmethod
    def from_values(nz: int, ny: int, nx: int, values: Iterable[float]) -> Slab3DF32:
        return Slab3DF32(nz=nz, ny=ny, nx=nx, buf=array("f", values))

    def at(self, k: int, j: int, i: int) -> float:
        return float(self.buf[(k * self.ny + j) * self.nx + i])


CompMass2D = NewType("CompMass2D", Slab2DF32)
PubMass2D = NewType("PubMass2D", Slab2DF32)

CompBucket2D = NewType("CompBucket2D", Slab2DI32)
PubBucket2D = NewType("PubBucket2D", Slab2DI32)

CompStagX3D = NewType("CompStagX3D", Slab3DF32)
CompStagY3D = NewType("CompStagY3D", Slab3DF32)
CompStagZ3D = NewType("CompStagZ3D", Slab3DF32)
CompMass3D = NewType("CompMass3D", Slab3DF32)
PubMass3D = NewType("PubMass3D", Slab3DF32)

__all__ = [
    "CompBucket2D",
    "CompMass2D",
    "CompMass3D",
    "CompStagX3D",
    "CompStagY3D",
    "CompStagZ3D",
    "PubBucket2D",
    "PubMass2D",
    "PubMass3D",
    "Slab2DF32",
    "Slab2DI32",
    "Slab3DF32",
]
