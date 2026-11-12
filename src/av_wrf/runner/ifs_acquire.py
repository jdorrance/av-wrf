"""Acquire indexed ECMWF IFS GRIB2 and emit regular-grid WPS intermediates."""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import cast

import httpx
import numpy as np
from numpy.typing import NDArray

from av_wrf.codecs import WpsIntermediateField, write_wps_intermediate_field
from av_wrf.core import ifs_dewpoint_to_spfh_vec
from av_wrf.runner.bundle import LoadedBundle
from av_wrf.runner.forcing_ifs import (
    AIR,
    LEVELS,
    REQUIRED,
    SOIL_SUFFIXES,
    SOURCE_UNITS,
    SURFACE,
    SURFACE_HEIGHTS_M,
)
from av_wrf.runner.http_request import _request
from av_wrf.runner.mirrors import ForcingSubset
from av_wrf.runner.range_fetch import _check_source, _receive_ranges, _source_info
from av_wrf.runner.store import LocalFileStore, json_bytes, put_idempotent

type _Key = tuple[str, int]
type _Bounds = tuple[float, float, float, float]
_DERIVED = {"2d": "sp", "rsn": "sd"}


@dataclass(frozen=True)
class _Entry:
    key: _Key
    offset: int
    length: int


def _wanted(kind: str) -> set[_Key]:
    if kind == "pl":
        return {(param, level) for param in AIR for level in LEVELS}
    return {(param, 0) for param in SURFACE} | {
        (param, level) for param in ("sot", "vsw") for level in range(1, 5)
    }


def _index(text: str, wanted: set[_Key], size: int) -> list[_Entry]:
    selected: list[_Entry] = []
    end = 0
    for line in text.splitlines():
        row = json.loads(line)
        if not isinstance(row, dict):
            raise ValueError("Invalid IFS index entry")
        offset, length = row.get("_offset"), row.get("_length")
        if type(offset) is not int or type(length) is not int or offset != end or length < 20:
            raise ValueError("Invalid or noncontiguous IFS index")
        end = offset + length
        if end > size:
            raise ValueError("IFS index exceeds object size")
        param, level = row.get("param"), row.get("levelist", "0")
        try:
            key = (str(param), int(level))
        except (ValueError, TypeError) as exc:
            raise ValueError("Invalid IFS index level") from exc
        if key in wanted:
            selected.append(_Entry(key, offset, length))
    if end != size or {entry.key for entry in selected} != wanted or len(selected) != len(wanted):
        raise ValueError("Incomplete or duplicate IFS indexed fields")
    return selected


def _download(client: httpx.Client, url: str, wanted: set[_Key]) -> dict[_Key, bytes]:
    size, etag = _source_info(client, url)
    index_url = url[:-6] + ".index"
    index = _request(client, "GET", index_url).text
    entries = _index(index, wanted, size)
    messages: dict[_Key, bytes] = {}
    chunks, _ = _receive_ranges(
        client, url, [(e.offset, e.offset + e.length - 1) for e in entries], size, etag
    )
    for entry, raw in zip(entries, chunks, strict=True):
        if (
            raw[:4] != b"GRIB"
            or raw[7] != 2
            or int.from_bytes(raw[8:16], "big") != len(raw)
            or raw[-4:] != b"7777"
        ):
            raise ValueError("Invalid IFS GRIB2 framing")
        messages[entry.key] = raw
    if _request(client, "GET", index_url).text != index:
        raise ValueError("IFS index changed during acquisition")
    _check_source(client, url, size, etag)
    return messages


def _fetch(
    client: httpx.Client, mirrors: tuple[str, ...], key: str, wanted: set[_Key]
) -> dict[_Key, bytes]:
    errors = []
    for mirror in mirrors:
        try:
            return _download(client, f"{mirror.rstrip('/')}/{key}", wanted)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            errors.append(f"{mirror}: {exc}")
    raise ValueError("All IFS mirrors failed: " + "; ".join(errors))


def _crop(gid: int, bounds: _Bounds, spacing: float) -> NDArray[np.float64]:
    import eccodes as ec

    nx, ny = int(ec.codes_get_long(gid, "Ni")), int(ec.codes_get_long(gid, "Nj"))
    lat = float(ec.codes_get_double(gid, "latitudeOfFirstGridPointInDegrees"))
    lon = float(ec.codes_get_double(gid, "longitudeOfFirstGridPointInDegrees"))
    dx = float(ec.codes_get_double(gid, "iDirectionIncrementInDegrees"))
    dy = float(ec.codes_get_double(gid, "jDirectionIncrementInDegrees"))
    if (
        ec.codes_get_string(gid, "gridType") != "regular_ll"
        or nx < 2
        or ny < 2
        or abs(dx - spacing) > 1e-6
        or abs(dy - spacing) > 1e-6
        or any(
            ec.codes_get_long(gid, k) != 0
            for k in (
                "iScansNegatively",
                "jScansPositively",
                "jPointsAreConsecutive",
                "alternativeRowScanning",
                "numberOfMissing",
                "uvRelativeToGrid",
            )
        )
        or abs(nx * dx - 360) > 1e-5
        or abs(lat - 90) > 1e-5
        or abs((ny - 1) * dy - 180) > 1e-5
    ):
        raise ValueError("Unsupported or incomplete IFS global grid")
    west, south, east, north = bounds
    x = np.rint((west - lon) / dx + np.arange(round((east - west) / dx) + 1)).astype(int) % nx
    y = np.rint((lat - south) / dy - np.arange(round((north - south) / dy) + 1)).astype(int)
    if (
        y.min() < 0
        or y.max() >= ny
        or abs((west - lon) / dx - round((west - lon) / dx)) > 1e-5
        or abs((lat - south) / dy - round((lat - south) / dy)) > 1e-5
    ):
        raise ValueError("IFS grid does not cover the requested cutout")
    if ec.codes_get_size(gid, "values") != nx * ny:
        raise ValueError("Incorrect IFS grid data size")
    if ec.codes_get_string(gid, "packingType") == "grid_simple":
        # ecCodes can seek directly into simple packing. CCSDS indexed reads decode the globe.
        # https://github.com/ecmwf/eccodes/blob/2.50.0/src/eccodes/accessor/DataCcsdsPacking.cc
        indices = (y[:, None] * nx + x).ravel().tolist()
        result = np.asarray(ec.codes_get_double_elements(gid, "values", indices)).reshape(
            y.size, x.size
        )
    else:
        values = np.asarray(ec.codes_get_values(gid), dtype=np.float64)
        result = values.reshape(ny, nx)[np.ix_(y, x)]
    if not np.isfinite(result).all():
        raise ValueError("Nonfinite IFS cutout")
    return result


def _normalize(
    raw: bytes, key: _Key, cycle: datetime, hour: int, bounds: _Bounds, spacing: float
) -> tuple[str, str, float, NDArray[np.float64]]:
    import eccodes as ec

    gid = cast(int, ec.codes_new_from_message(raw))
    try:
        param, level = key
        valid = cycle + timedelta(hours=hour)
        expected = {
            "edition": 2,
            "dataDate": int(cycle.strftime("%Y%m%d")),
            "dataTime": int(cycle.strftime("%H%M")),
            "validityDate": int(valid.strftime("%Y%m%d")),
            "validityTime": int(valid.strftime("%H%M")),
        }
        if (
            ec.codes_get_string(gid, "centre") != "ecmf"
            or ec.codes_get_string(gid, "shortName") != param
            or ec.codes_get_string(gid, "units") != SOURCE_UNITS[param]
            or any(ec.codes_get_long(gid, k) != v for k, v in expected.items())
        ):
            raise ValueError(f"Wrong IFS field, cycle or valid time: {key}")
        level_type = ec.codes_get_string(gid, "typeOfLevel")
        if param in AIR:
            if level_type != "isobaricInhPa" or ec.codes_get_long(gid, "level") != level:
                raise ValueError(f"Wrong IFS pressure level: {key}")
            name, units = AIR[param]
            output_level = float(level * 100)
        elif param in ("sot", "vsw"):
            if level_type != "soilLayer" or ec.codes_get_long(gid, "level") != level:
                raise ValueError(f"Wrong IFS soil level: {key}")
            name = ("ST" if param == "sot" else "SM") + SOIL_SUFFIXES[level - 1]
            units, output_level = ("K" if param == "sot" else "m3 m-3"), 200100.0
        else:
            expected_level = SURFACE_HEIGHTS_M.get(param, 0)
            surface_type = "heightAboveGround" if expected_level else "surface"
            if level_type != ("meanSea" if param == "msl" else surface_type) or (
                ec.codes_get_long(gid, "level") != expected_level
            ):
                raise ValueError(f"Wrong IFS surface level: {key}")
            name, units = SURFACE[param]
            output_level = 201300.0 if param == "msl" else 200100.0
        values = _crop(gid, bounds, spacing)
        # ECMWF z is geopotential and sd is metres of water equivalent.
        # https://codes.ecmwf.int/grib/param-db/129 https://codes.ecmwf.int/grib/param-db/141
        if factor := {"z": 1 / 9.80665, "sd": 1000.0}.get(param):
            values *= factor
        elif param == "lsm":
            if np.any((values < 0) | (values > 1)):
                raise ValueError("IFS land fraction is outside [0, 1]")
            # WPS ungrib thresholds the ECMWF land fraction for exact 0/1 donor masks.
            # https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/src/rrpr.F#L901-L916
            values = (values > 0.5).astype(np.float64)
        return name, units, output_level, values
    finally:
        ec.codes_release(gid)


def _snow_depth_m(
    density_kg_m3: NDArray[np.float64], swe_kg_m2: NDArray[np.float64]
) -> NDArray[np.float64]:
    if np.any((swe_kg_m2 < 0) | ((swe_kg_m2 > 0) & (density_kg_m3 <= 0))):
        raise ValueError("IFS snow requires nonnegative water equivalent and positive density")
    # WPS derives physical snow depth from water equivalent and ECMWF snow density.
    # https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/src/rrpr.F#L836-L855
    return np.divide(swe_kg_m2, density_kg_m3, out=np.zeros_like(swe_kg_m2), where=swe_kg_m2 > 0)


def _write_hour(
    cycle: datetime,
    hour: int,
    bounds: _Bounds,
    spacing: float,
    radius: float,
    fields: list[tuple[_Key, bytes, int]],
) -> bytes:
    stream = BytesIO()
    seen = set()
    operands: dict[str, NDArray[np.float64]] = {}
    valid = (cycle + timedelta(hours=hour)).strftime("%Y-%m-%d_%H:%M:%S")
    # Emit prerequisites first regardless of GRIB message order.
    for key, raw, source_hour in sorted(fields, key=lambda row: row[0][0] in _DERIVED):
        name, units, level, values = _normalize(raw, key, cycle, source_hour, bounds, spacing)
        param = key[0]
        if param in _DERIVED.values():
            operands[param] = values
        if (operand := _DERIVED.get(param)) is not None:
            if operand not in operands:
                raise ValueError(f"Missing IFS surface prerequisite {operand} for {param}")
            derive = ifs_dewpoint_to_spfh_vec if param == "2d" else _snow_depth_m
            values = derive(values, operands[operand])
        identity = (name, level)
        if identity in seen or identity not in REQUIRED:
            raise ValueError(f"Unexpected or duplicate IFS field {identity}")
        seen.add(identity)
        field = WpsIntermediateField(
            valid,
            float(hour),
            "ECMWF IFS",
            name,
            units,
            name,
            level,
            bounds[1],
            bounds[0],
            spacing,
            spacing,
            radius / 1000.0,
            False,
        )
        write_wps_intermediate_field(stream, field, values)
    if seen != REQUIRED:
        raise ValueError(f"Missing IFS fields: {sorted(REQUIRED - seen)}")
    return stream.getvalue()


def acquire_ifs(
    client: httpx.Client,
    bundle: LoadedBundle,
    cycle: datetime,
    plan: ForcingSubset,
    destination: Path,
) -> dict[int, str]:
    """Acquire and write one validated WPS file per boundary, plus source hashes."""
    hours = plan["forecast_hours"]
    bounds = cast(_Bounds, tuple(plan["bbox_wsen_deg"]))
    keys = plan["file_keys"][str(hours[0])]
    if not isinstance(keys, list):
        raise ValueError("Expected IFS object inventory")
    fixed_key = keys[-1]
    mirrors = bundle.primary_source.mirrors
    wanted_surface = _wanted("sfc")
    terrain_key = ("z", 0)
    fixed = _fetch(client, mirrors, fixed_key, {terrain_key} if hours[0] else wanted_surface)
    source_shas: dict[str, str] = {}
    normalized_shas: dict[str, str] = {}
    files: dict[int, str] = {}
    store = LocalFileStore(destination)
    for hour in hours:
        group = plan["file_keys"][str(hour)]
        if not isinstance(group, list) or len(group) not in (2, 3) or group[-1] != fixed_key:
            raise ValueError("Invalid IFS object inventory")
        pressure = _fetch(client, mirrors, group[0], _wanted("pl"))
        surface = (
            _fetch(client, mirrors, group[1], wanted_surface - {terrain_key}) if hour else fixed
        )
        for key, messages in ((fixed_key, fixed), (group[0], pressure), (group[1], surface)):
            if key not in source_shas:
                digest = sha256()
                for raw in messages.values():
                    digest.update(raw)
                source_shas[key] = digest.hexdigest()
        fields = [(key, raw, hour) for key, raw in pressure.items()]
        fields += [(key, raw, hour) for key, raw in surface.items() if key != terrain_key]
        fields.append((terrain_key, fixed[terrain_key], 0))
        data = _write_hour(
            cycle,
            hour,
            bounds,
            bundle.primary_source.grid_spacing_deg,
            bundle.domain.earth_radius_m,
            fields,
        )
        name = f"ifs.{cycle:%Y%m%d%H}.f{hour:03d}.wps"
        normalized_shas[name] = put_idempotent(store, name, data)
        files[hour] = name
    inventory = {
        "forcing_cycle_utc": cycle.isoformat(),
        "source_sha256s": source_shas,
        "normalized_sha256s": normalized_shas,
    }
    put_idempotent(
        store, f"ifs.{cycle:%Y%m%d%H}.sources-f{hours[-1]:03d}.json", json_bytes(inventory)
    )
    return files


__all__ = ["acquire_ifs"]
