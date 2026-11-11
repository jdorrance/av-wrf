"""Nominal physical quantity, reference-frame, and contiguous float32 slab types."""

from __future__ import annotations

from array import array
from collections.abc import Iterable
from dataclasses import dataclass
from typing import NewType

Meters = NewType("Meters", float)
Pascals = NewType("Pascals", float)
TempK = NewType("TempK", float)
WrfPerturbPotTempK = NewType("WrfPerturbPotTempK", float)
WrfPerturbMoistThetaK = NewType("WrfPerturbMoistThetaK", float)
MixingRatioKgKg = NewType("MixingRatioKgKg", float)
SpecificHumidityKgKg = NewType("SpecificHumidityKgKg", float)
AccumulatedJouleM2 = NewType("AccumulatedJouleM2", float)
MeanFluxWm2 = NewType("MeanFluxWm2", float)
AccumulatedPrecipMm = NewType("AccumulatedPrecipMm", float)
PrecipRateMmHr = NewType("PrecipRateMmHr", float)
GridWindMps = NewType("GridWindMps", float)
EarthWindMps = NewType("EarthWindMps", float)
VerticalVelocityMps = NewType("VerticalVelocityMps", float)
OmegaPaS = NewType("OmegaPaS", float)


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


CompStagX2D = NewType("CompStagX2D", Slab2DF32)
CompStagY2D = NewType("CompStagY2D", Slab2DF32)
CompMass2D = NewType("CompMass2D", Slab2DF32)
PubMass2D = NewType("PubMass2D", Slab2DF32)

CompStagX3D = NewType("CompStagX3D", Slab3DF32)
CompStagY3D = NewType("CompStagY3D", Slab3DF32)
CompStagZ3D = NewType("CompStagZ3D", Slab3DF32)
CompMass3D = NewType("CompMass3D", Slab3DF32)
PubMass3D = NewType("PubMass3D", Slab3DF32)

__all__ = [
    "AccumulatedJouleM2",
    "AccumulatedPrecipMm",
    "CompMass2D",
    "CompMass3D",
    "CompStagX2D",
    "CompStagX3D",
    "CompStagY2D",
    "CompStagY3D",
    "CompStagZ3D",
    "EarthWindMps",
    "GridWindMps",
    "MeanFluxWm2",
    "Meters",
    "MixingRatioKgKg",
    "OmegaPaS",
    "Pascals",
    "PrecipRateMmHr",
    "PubMass2D",
    "PubMass3D",
    "Slab2DF32",
    "Slab3DF32",
    "SpecificHumidityKgKg",
    "TempK",
    "VerticalVelocityMps",
    "WrfPerturbMoistThetaK",
    "WrfPerturbPotTempK",
]
