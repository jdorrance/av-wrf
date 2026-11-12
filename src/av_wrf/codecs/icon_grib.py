"""Remap DWD cell-centered GRIB2 to a WPS regular latitude/longitude cutout."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import isclose, isfinite
from typing import BinaryIO, cast

import numpy as np
from numpy.typing import NDArray

from av_wrf.codecs.icon_grid import IconGrid, icon_grid
from av_wrf.codecs.wrf_nc_grid import masked_weights

type _FloatArray = NDArray[np.float64]
type _Field = tuple[int, int, int, int]


@contextmanager
def _message(raw: bytes, name: str = "normalized invariant") -> Iterator[int]:
    import eccodes as ec

    try:
        gid = cast(int, ec.codes_new_from_message(raw))
        try:
            if ec.codes_get_message_size(gid) != len(raw):
                raise ValueError("Expected one complete ICON GRIB2 message per object")
            yield gid
        finally:
            ec.codes_release(gid)
    except (ec.CodesInternalError, ValueError) as exc:
        raise ValueError(f"Invalid ICON GRIB2 object {name}: {exc}") from exc


def _identity(gid: int, cycle: datetime, hour: int) -> tuple[str, int]:
    import eccodes as ec

    valid = cycle + timedelta(hours=hour)
    expected = {
        "edition": 2,
        "centre": 78,
        "generatingProcessIdentifier": 2,
        "productDefinitionTemplateNumber": 0,
        "dataDate": int(cycle.strftime("%Y%m%d")),
        "dataTime": int(cycle.strftime("%H%M")),
        "validityDate": int(valid.strftime("%Y%m%d")),
        "validityTime": int(valid.strftime("%H%M")),
    }
    if any(ec.codes_get_long(gid, key) != value for key, value in expected.items()):
        raise ValueError(f"Wrong DWD ICON GRIB2 identity or valid time for f{hour:03d}")
    if ec.codes_get_string(gid, "gridType") != "unstructured_grid":
        raise ValueError("Expected the DWD native triangular grid")
    uuid = cast(str, ec.codes_get_string(gid, "uuidOfHGrid"))
    return uuid, int(ec.codes_get_long(gid, "numberOfDataPoints"))


def _field(gid: int) -> _Field:
    import eccodes as ec

    return (
        int(ec.codes_get_long(gid, "discipline")),
        int(ec.codes_get_long(gid, "parameterCategory")),
        int(ec.codes_get_long(gid, "parameterNumber")),
        int(ec.codes_get_long(gid, "typeOfFirstFixedSurface")),
    )


def verify_icon_object(
    raw: bytes, key: str, cycle: datetime, hour: int, selector: _Field, level: float
) -> None:
    """Unpack native ICON data and check its key identity before cache admission."""
    import eccodes as ec

    with _message(raw, key) as gid:
        _, count = _identity(gid, cycle, hour)
        if _field(gid) != selector or not isclose(ec.codes_get_double(gid, "level"), level):
            raise ValueError("Wrong ICON GRIB2 selector or level")
        if (
            count < 1
            or ec.codes_get_size(gid, "values") != count
            or ec.codes_get_long(gid, "numberOfMissing") != 0
            or not all(isfinite(ec.codes_get_double(gid, k)) for k in ("min", "max", "average"))
        ):
            raise ValueError("Incomplete or nonfinite ICON native field")


def _coordinates(
    key: str, read_object: Callable[[str], bytes], cycle: datetime, code: _Field
) -> tuple[str, _FloatArray]:
    import eccodes as ec

    with _message(read_object(key), key) as gid:
        uuid, count = _identity(gid, cycle, 0)
        values = np.asarray(ec.codes_get_values(gid), dtype=np.float64)
        if _field(gid) != code or values.shape != (count,) or not np.isfinite(values).all():
            raise ValueError("Invalid ICON coordinates")
    return uuid, values


def _values(gid: int, cutout: IconGrid, cycle: datetime, hour: int) -> _FloatArray:
    import eccodes as ec

    if _identity(gid, cycle, hour) != (cutout.uuid, cutout.count):
        raise ValueError("ICON field grid differs from the coordinate grid")
    values = np.asarray(ec.codes_get_values(gid), dtype=np.float64)
    if values.shape != (cutout.count,) or ec.codes_get_long(gid, "numberOfMissing") != 0:
        raise ValueError("Incomplete ICON native field")
    donors = values[cutout.donors]
    if not np.isfinite(donors).all():
        raise ValueError("Nonfinite ICON values in the WPS cutout")
    return donors


def _encode(gid: int, cutout: IconGrid, values: _FloatArray) -> bytes:
    import eccodes as ec

    if not np.isfinite(values).all():
        raise ValueError("Nonfinite values in the normalized ICON field")
    # ecCodes decodes surface 150 as a six-value general vertical coordinate.
    if (
        ec.codes_get_long(gid, "typeOfFirstFixedSurface") == 150
        and ec.codes_get_long(gid, "NV") != 6
    ):
        raise ValueError("ICON hybrid field requires six vertical coordinate values")
    for key, value in {
        "gridType": "regular_ll",
        "Ni": cutout.nx,
        "Nj": cutout.ny,
        "latitudeOfFirstGridPointInDegrees": cutout.south,
        "latitudeOfLastGridPointInDegrees": cutout.north,
        "longitudeOfFirstGridPointInDegrees": cutout.west % 360.0,
        "longitudeOfLastGridPointInDegrees": cutout.east % 360.0,
        "iDirectionIncrementInDegrees": cutout.spacing,
        "jDirectionIncrementInDegrees": cutout.spacing,
        "iScansNegatively": 0,
        "jScansPositively": 1,
        "jPointsAreConsecutive": 0,
        "alternativeRowScanning": 0,
        "uvRelativeToGrid": 0,
        "packingType": "grid_simple",
        "bitsPerValue": 24,
        "bitmapPresent": 0,
    }.items():
        ec.codes_set(gid, key, value)
    ec.codes_set_values(gid, values)
    return cast(bytes, ec.codes_get_message(gid))


def _weighted(donors: _FloatArray, weights: _FloatArray) -> _FloatArray:
    return np.asarray(np.einsum("ni,ni->n", donors, weights), dtype=np.float64)


@dataclass(frozen=True)
class IconField:
    """One remapped slab and its original GRIB selector and grid metadata."""

    selector: _Field
    level: float
    bottom: float
    raw_top: int
    radius_km: float
    grid: IconGrid
    values: _FloatArray


def _slab(gid: int, grid: IconGrid, values: _FloatArray) -> IconField:
    import eccodes as ec

    selector = _field(gid)
    if selector[3] == 150 and ec.codes_get_long(gid, "NV") != 6:
        raise ValueError("ICON hybrid field requires six vertical coordinate values")
    if not np.isfinite(values).all():
        raise ValueError("Nonfinite values in the normalized ICON field")
    return IconField(
        selector,
        float(ec.codes_get_double(gid, "level")),
        float(ec.codes_get_double(gid, "bottomLevel")),
        int(ec.codes_get_long(gid, "scaledValueOfFirstFixedSurface")),
        6371.229,  # ecCodes regular_ll template uses the WMO GRIB2 spherical Earth radius.
        grid,
        values.reshape(grid.ny, grid.nx),
    )


def _fixed_slabs(
    fields: Mapping[str, Sequence[str]],
    read: Callable[[str], bytes],
    cycle: datetime,
    grid: IconGrid,
    selector: _Field,
    land: _FloatArray,
    terrain: _FloatArray,
) -> tuple[tuple[bytes, IconField], ...]:
    import eccodes as ec

    slabs = []
    previous: _FloatArray | None = None
    keys = (fields["FR_LAND"][0], fields["HSURF"][0], *fields["HHL"])
    for index, key in enumerate(keys):
        with _message(read(key), key) as gid:
            values = land if index == 0 else terrain
            if index >= 2:
                level = index - 1
                height = _weighted(_values(gid, grid, cycle, 0), grid.weights)
                if _field(gid) != selector or ec.codes_get_long(gid, "level") != level:
                    raise ValueError(f"Wrong ICON height interface {level}")
                if previous is None:
                    previous = height
                    continue
                if np.any(height >= previous):
                    raise ValueError("ICON height interfaces must descend toward the surface")
                values, previous = 0.5 * (previous + height), height
                ec.codes_set(gid, "scaledValueOfFirstFixedSurface", level - 1)
                ec.codes_set(gid, "typeOfSecondFixedSurface", 150)
                ec.codes_set(gid, "scaledValueOfSecondFixedSurface", level)
            slab = _slab(gid, grid, values)
            slab.values.setflags(write=False)
            # Keep only cropped data and a constant-size template for legacy GRIB output.
            ec.codes_set_values(gid, np.zeros(1))
            slabs.append((cast(bytes, ec.codes_get_message(gid)), slab))
    if previous is None or not np.allclose(previous, terrain, rtol=0.0, atol=0.25):
        raise ValueError("ICON bottom height interface disagrees with source terrain")
    return tuple(slabs)


@dataclass(frozen=True)
class IconGribCutout:
    """Remapping weights and cropped immutable invariants for one DWD cycle."""

    _cutout: IconGrid
    _cycle: datetime
    _surface_weights: _FloatArray
    _soil_weights: _FloatArray
    _fixed: tuple[tuple[bytes, IconField], ...]
    _selectors: Mapping[str, _Field]
    _read_object: Callable[[str], bytes]

    @classmethod
    def from_native(
        cls,
        fields: Mapping[str, Sequence[str]],
        read_object: Callable[[str], bytes],
        cycle: datetime,
        bounds: tuple[float, float, float, float],
        spacing: float,
        selectors: Mapping[str, _Field],
    ) -> IconGribCutout:
        """Read DWD invariant fields by object key and precompute interpolation weights."""
        lat_uuid, lat = _coordinates(fields["CLAT"][0], read_object, cycle, selectors["CLAT"])
        lon_uuid, lon = _coordinates(fields["CLON"][0], read_object, cycle, selectors["CLON"])
        if lat_uuid != lon_uuid or lat.shape != lon.shape or np.max(np.abs(lat)) > 90.0:
            raise ValueError("Mismatched ICON latitude and longitude grid")
        cutout = icon_grid(lat_uuid, lat, lon, bounds, spacing)
        land_key = fields["FR_LAND"][0]
        with _message(read_object(land_key), land_key) as gid:
            land = _values(gid, cutout, cycle, 0)
            if _field(gid) != selectors["FR_LAND"] or np.min(land) < 0.0 or np.max(land) > 1.0:
                raise ValueError("Invalid ICON land fraction")
            target_land = _weighted(land, cutout.weights) >= 0.5
        same_surface = (land >= 0.5) == target_land[:, None]
        surface_weights = masked_weights(cutout.weights, same_surface.astype(bool))
        soil_weights = masked_weights(cutout.weights, land > 0.0)
        terrain_key = fields["HSURF"][0]
        with _message(read_object(terrain_key), terrain_key) as gid:
            if _field(gid) != selectors["HSURF"]:
                raise ValueError("Invalid ICON terrain field")
            terrain = _weighted(_values(gid, cutout, cycle, 0), cutout.weights)
        return cls(
            cutout,
            cycle,
            surface_weights,
            soil_weights,
            _fixed_slabs(
                fields,
                read_object,
                cycle,
                cutout,
                selectors["HHL"],
                target_land.astype(np.float64),
                terrain,
            ),
            selectors,
            read_object,
        )

    def write_hour(
        self,
        stream: BinaryIO,
        fields: Mapping[str, Sequence[str]],
        hour: int,
        top_pa: float,
    ) -> None:
        """Write a normalized WPS GRIB2 stream for one boundary hour."""
        import eccodes as ec

        for gid, slab in self._slabs(fields, hour, top_pa):
            ec.codes_set(gid, "indicatorOfUnitOfTimeRange", 0)
            ec.codes_set(gid, "forecastTime", hour * 60)
            stream.write(_encode(gid, self._cutout, slab.values.ravel()))

    def fields(
        self, fields: Mapping[str, Sequence[str]], hour: int, top_pa: float
    ) -> Iterator[IconField]:
        """Yield one remapped slab at a time for a boundary hour."""
        for _, slab in self._slabs(fields, hour, top_pa):
            yield slab

    def _slabs(
        self, fields: Mapping[str, Sequence[str]], hour: int, top_pa: float
    ) -> Iterator[tuple[int, IconField]]:
        import eccodes as ec

        for template, slab in self._fixed:
            with _message(template) as gid:
                yield gid, slab
        last_pressure: _FloatArray | None = None
        for field, keys in fields.items():
            for key in keys:
                with _message(self._read_object(key), key) as gid:
                    if _field(gid) != self._selectors[field]:
                        raise ValueError(f"Wrong ICON GRIB2 selector for {field} at f{hour:03d}")
                    donors = _values(gid, self._cutout, self._cycle, hour)
                    if field == "H_ICE":
                        if np.any(donors != 0.0):
                            raise ValueError("ICON cutout requires fraction for nonzero sea ice")
                        ec.codes_set(gid, "parameterNumber", 0)
                    weights = (
                        self._soil_weights
                        if field in {"T_SO", "W_SO"}
                        else self._surface_weights
                        if field in {"T_G", "H_SNOW", "W_SNOW"}
                        else self._cutout.weights
                    )
                    values = _weighted(donors, weights)
                    if field == "P":
                        valid = (
                            np.min(values) > 0.0 and np.max(values) < top_pa
                            if last_pressure is None
                            else np.all(values > last_pressure)
                        )
                        if not valid:
                            raise ValueError("ICON pressures must bracket model top and increase")
                        last_pressure = values
                    yield gid, _slab(gid, self._cutout, values)


__all__ = ["IconField", "IconGribCutout", "verify_icon_object"]
