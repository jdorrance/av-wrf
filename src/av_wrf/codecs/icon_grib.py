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

type _FloatArray = NDArray[np.float64]
type _IntArray = NDArray[np.intp]
type _Field = tuple[int, int, int, int]


@dataclass(frozen=True)
class _Cutout:
    west: float
    south: float
    east: float
    north: float
    spacing: float
    nx: int
    ny: int
    uuid: str
    count: int
    donors: _IntArray
    weights: _FloatArray


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


def _normalize_weights(weights: _FloatArray, context: str) -> None:
    support = weights.sum(axis=1, keepdims=True)
    if not np.all(np.isfinite(support) & (support > 0.0)):
        raise ValueError(f"Invalid ICON interpolation weights for {context}")
    weights /= support


def _cutout(
    coordinates: tuple[str, str],
    read_object: Callable[[str], bytes],
    cycle: datetime,
    bounds: tuple[float, float, float, float],
    spacing: float,
    selectors: Mapping[str, _Field],
) -> _Cutout:
    from scipy.spatial import Delaunay

    lat_uuid, lat = _coordinates(coordinates[0], read_object, cycle, selectors["CLAT"])
    lon_uuid, lon = _coordinates(coordinates[1], read_object, cycle, selectors["CLON"])
    if lat_uuid != lon_uuid or lat.shape != lon.shape or np.max(np.abs(lat)) > 90.0:
        raise ValueError("Mismatched ICON latitude and longitude grid")
    west, south, east, north = bounds
    middle = 0.5 * (west + east)
    lon = middle + (lon - middle + 180.0) % 360.0 - 180.0
    # Triangulate the nearby circumcenters in a local metric. DWD documents
    # barycentric interpolation and Delaunay circumcenter meshes in ICON Tools.
    # https://www.dwd.de/EN/ourservices/nwp_icon_tutorial/nwp_icon_tutorial.html
    margin = 1.0
    nearby = np.flatnonzero(
        (lon >= west - margin)
        & (lon <= east + margin)
        & (lat >= south - margin)
        & (lat <= north + margin)
    )
    if nearby.size < 3:
        raise ValueError("ICON grid does not cover the WPS cutout")
    lon_scale = float(np.cos(np.deg2rad(0.5 * (south + north))))
    donors = np.column_stack((lon[nearby] * lon_scale, lat[nearby]))
    nx, ny = round((east - west) / spacing) + 1, round((north - south) / spacing) + 1
    gx, gy = np.meshgrid(west + spacing * np.arange(nx), south + spacing * np.arange(ny))
    targets = np.column_stack((gx.ravel() * lon_scale, gy.ravel()))
    mesh = Delaunay(donors)
    simplex = np.asarray(mesh.find_simplex(targets), dtype=np.intp)
    if np.any(simplex < 0):
        raise ValueError("ICON grid does not cover the full WPS cutout")
    vertices = np.asarray(mesh.simplices[simplex], dtype=np.intp)
    separation = np.linalg.norm(donors[vertices] - targets[:, None, :], axis=2)
    if np.max(separation) > 3.0 * spacing:
        raise ValueError("ICON grid has a gap in the WPS cutout")
    transforms = np.asarray(mesh.transform[simplex], dtype=np.float64)
    local = np.einsum("nij,nj->ni", transforms[:, :2], targets - transforms[:, 2])
    weights = np.maximum(np.column_stack((local, 1.0 - local.sum(axis=1))), 0.0)
    _normalize_weights(weights, "the native grid")
    return _Cutout(
        west, south, east, north, spacing, nx, ny, lat_uuid, lat.size, nearby[vertices], weights
    )


def _values(gid: int, cutout: _Cutout, cycle: datetime, hour: int) -> _FloatArray:
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


def _encode(gid: int, cutout: _Cutout, values: _FloatArray) -> bytes:
    import eccodes as ec

    if not np.isfinite(values).all():
        raise ValueError("Nonfinite values in the normalized ICON field")
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
        # DWD's cell-centered U/V are zonal and meridional; VN is edge-normal.
        "uvRelativeToGrid": 0,
        # WPS 4.7's g2 reader lacks CCSDS unpacking.
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
class IconGribCutout:
    """Remapping weights and prepared invariant GRIB2 messages for one DWD cycle."""

    _cutout: _Cutout
    _cycle: datetime
    _surface_weights: _FloatArray
    _constant: tuple[bytes, ...]
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
        cutout = _cutout(
            (fields["CLAT"][0], fields["CLON"][0]), read_object, cycle, bounds, spacing, selectors
        )
        land_key = fields["FR_LAND"][0]
        with _message(read_object(land_key), land_key) as gid:
            land = _values(gid, cutout, cycle, 0)
            if _field(gid) != selectors["FR_LAND"] or np.min(land) < 0.0 or np.max(land) > 1.0:
                raise ValueError("Invalid ICON land fraction")
            target_land = _weighted(land, cutout.weights) >= 0.5
            mask = _encode(gid, cutout, target_land.astype(np.float64))
        same_surface = (land >= 0.5) == target_land[:, None]
        surface_weights = cutout.weights * same_surface
        _normalize_weights(surface_weights, "the target land/sea class")
        terrain_key = fields["HSURF"][0]
        with _message(read_object(terrain_key), terrain_key) as gid:
            if _field(gid) != selectors["HSURF"]:
                raise ValueError("Invalid ICON terrain field")
            terrain = _weighted(_values(gid, cutout, cycle, 0), cutout.weights)
            terrain_grib = _encode(gid, cutout, terrain)
        heights = _full_heights(
            fields["HHL"], read_object, cutout, cycle, terrain, selectors["HHL"]
        )
        return cls(
            cutout, cycle, surface_weights, (mask, terrain_grib, *heights), selectors, read_object
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

        for message in self._constant:
            with _message(message) as gid:
                # ICON encodes lead time in minutes, including analysis messages.
                ec.codes_set(gid, "forecastTime", hour * 60)
                ec.codes_write(gid, stream)
        last_pressure: _FloatArray | None = None
        for field, keys in fields.items():
            for key in keys:
                with _message(self._read_object(key), key) as gid:
                    values = self._remap(gid, field, hour)
                    if field == "P":
                        _check_pressure(values, last_pressure, top_pa)
                        last_pressure = values
                    stream.write(_encode(gid, self._cutout, values))

    def _remap(self, gid: int, field: str, hour: int) -> _FloatArray:
        import eccodes as ec

        if _field(gid) != self._selectors[field]:
            raise ValueError(f"Wrong ICON GRIB2 selector for {field} at f{hour:03d}")
        donors = _values(gid, self._cutout, self._cycle, hour)
        if field == "H_ICE":
            # Thickness cannot determine fractional coverage when any ice is present.
            if np.any(donors != 0.0):
                raise ValueError("ICON cutout requires sea-ice fraction for nonzero ice thickness")
            ec.codes_set(gid, "parameterNumber", 0)
        surface = field in {"T_G", "T_SO", "W_SO", "H_SNOW", "W_SNOW"}
        weights = self._surface_weights if surface else self._cutout.weights
        return _weighted(donors, weights)


def _check_pressure(values: _FloatArray, previous: _FloatArray | None, top_pa: float) -> None:
    if previous is None:
        if np.min(values) <= 0.0 or np.max(values) >= top_pa:
            raise ValueError("ICON upper pressure level does not bracket the WRF model top")
    elif np.any(values <= previous):
        raise ValueError("ICON pressure must increase toward the surface at each cutout cell")


def _full_heights(
    keys: Sequence[str],
    read_object: Callable[[str], bytes],
    cutout: _Cutout,
    cycle: datetime,
    terrain: _FloatArray,
    selector: _Field,
) -> tuple[bytes, ...]:
    import eccodes as ec

    messages = []
    previous: _FloatArray | None = None
    for level, key in enumerate(keys, start=1):
        with _message(read_object(key), key) as gid:
            height = _weighted(_values(gid, cutout, cycle, 0), cutout.weights)
            if _field(gid) != selector or ec.codes_get_long(gid, "level") != level:
                raise ValueError(f"Wrong ICON height interface {level}")
            if previous is not None:
                if np.any(height >= previous):
                    raise ValueError("ICON height interfaces must descend toward the surface")
                ec.codes_set(gid, "scaledValueOfFirstFixedSurface", level - 1)
                ec.codes_set(gid, "typeOfSecondFixedSurface", 150)
                ec.codes_set(gid, "scaledValueOfSecondFixedSurface", level)
                messages.append(_encode(gid, cutout, 0.5 * (previous + height)))
            previous = height
    if previous is None or not np.allclose(previous, terrain, rtol=0.0, atol=0.25):
        raise ValueError("ICON bottom height interface disagrees with source terrain")
    return tuple(messages)


__all__ = ["IconGribCutout", "verify_icon_object"]
