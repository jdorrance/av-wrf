"""Validate staged forcing GRIB2 files against their source recipe and RunLock."""

from __future__ import annotations

import json
import math
import subprocess
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

from av_wrf.contracts import DomainProfile, ForcingManifest, ForcingProfile, RunLock, parse_iso_utc
from av_wrf.core import (
    computational_latlon_bounds,
    required_forcing_forecast_hours,
)
from av_wrf.runner.store import LocalFileStore, json_bytes, put_idempotent, sha256_file

FORCING_MANIFEST_FILENAME = "forcing_manifest.json"

# GRIB2 codes from the WPS v4.7.0 Vtable.GFS and the NCEP GFS product inventory.
# https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/Variable_Tables/Vtable.GFS
_ATMOSPHERIC = ((0, 0, 0), (0, 1, 1), (0, 2, 2), (0, 2, 3), (0, 3, 5))
_SURFACE = (
    (0, 0, 0, 1, 0, 0),
    (0, 1, 13, 1, 0, 0),
    (0, 3, 0, 1, 0, 0),
    (0, 3, 5, 1, 0, 0),
    (0, 3, 192, 101, 0, 0),
    (2, 0, 0, 1, 0, 0),
    (10, 2, 0, 1, 0, 0),
    (0, 0, 0, 103, 2, 2),
    (0, 1, 1, 103, 2, 2),
    (0, 2, 2, 103, 10, 10),
    (0, 2, 3, 103, 10, 10),
)
_SOIL = tuple(
    (2, 0, param, 106, top, bottom)
    for param in (2, 192)
    for top, bottom in ((0, 0.1), (0.1, 0.4), (0.4, 1), (1, 2))
)
_FIELD_KEYS = (
    "discipline",
    "parameterCategory",
    "parameterNumber",
    "typeOfFirstFixedSurface",
    "level",
    "bottomLevel",
)
_GRIB_KEYS = (
    "edition,centre:i,generatingProcessIdentifier:i,productDefinitionTemplateNumber:i,"
    "dataDate:i,dataTime:i,validityDate:i,validityTime:i,"
    "discipline:i,parameterCategory:i,parameterNumber:i,typeOfFirstFixedSurface:i,level:d,"
    "bottomLevel:d,typeOfLevel,gridType,md5Section3,offset:i,totalLength:i,packingType,"
    "latitudeOfFirstGridPointInDegrees:d,latitudeOfLastGridPointInDegrees:d,"
    "longitudeOfFirstGridPointInDegrees:d,longitudeOfLastGridPointInDegrees:d,"
    "iDirectionIncrementInDegrees:d,jDirectionIncrementInDegrees:d,Ni:i,Nj:i"
)


def _inspect_grib(path: Path) -> list[dict[str, object]]:
    try:
        proc = subprocess.run(
            ["grib_ls", "-j", "-p", _GRIB_KEYS, str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("Install the ecCodes grib_ls tool to verify forcing") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(f"ecCodes rejected {path}: {exc.stderr.strip()}") from exc
    doc = json.loads(proc.stdout)
    messages = doc.get("messages") if isinstance(doc, dict) else None
    if (
        not isinstance(messages, list)
        or not messages
        or any(not isinstance(row, dict) for row in messages)
    ):
        raise ValueError(f"No readable GRIB2 messages in {path}")
    return messages


def _number(row: Mapping[str, object], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Missing or invalid GRIB2 key {key}")
    return float(value)


def _validate_gfs_coverage(
    row: Mapping[str, object], domain: DomainProfile, source: ForcingProfile
) -> None:
    w, s, e, n = computational_latlon_bounds(domain, snap_deg=source.grid_spacing_deg)
    dx = _number(row, "iDirectionIncrementInDegrees")
    dy = _number(row, "jDirectionIncrementInDegrees")
    ni, nj = _number(row, "Ni"), _number(row, "Nj")
    lon0 = _number(row, "longitudeOfFirstGridPointInDegrees")
    lon1 = _number(row, "longitudeOfLastGridPointInDegrees")
    south, north = sorted(
        (
            _number(row, "latitudeOfFirstGridPointInDegrees"),
            _number(row, "latitudeOfLastGridPointInDegrees"),
        )
    )
    width = dx * (ni - 1)
    west = w - (w - lon0) % 360.0
    if (
        row.get("gridType") != "regular_ll"
        or not math.isclose(dx, source.grid_spacing_deg, abs_tol=1e-6)
        or not math.isclose(dy, source.grid_spacing_deg, abs_tol=1e-6)
        or min(ni, nj) < 2
        or not math.isclose((lon1 - lon0) % 360.0, width, abs_tol=1e-5)
        or not math.isclose(north - south, dy * (nj - 1), abs_tol=1e-5)
        or (ni * dx < 360.0 - 1e-5 and e > west + width + 1e-5)
        or s < south - 1e-5
        or n > north + 1e-5
    ):
        raise ValueError("GFS grid geometry or coverage disagrees with the forcing cutout")


def _validate_gfs_inventory(
    fields: set[tuple[object, ...]], domain: DomainProfile, source: ForcingProfile
) -> None:
    levels = [{row[4] for row in fields if row[:4] == (*var, 100)} for var in _ATMOSPHERIC]
    pressure = sorted(float(x) for x in levels[0] if isinstance(x, (int, float)))
    if (
        len(pressure) < source.pressure_level_count - 1
        or any(group != levels[0] for group in levels[1:])
        or not pressure
        or pressure[0] > domain.p_top_pa
        or pressure[-1] < 100_000.0
    ):
        raise ValueError("Incomplete or inconsistent GFS isobaric field inventory")
    missing = set((*_SURFACE, *_SOIL)) - fields
    if missing:
        raise ValueError(f"Missing GFS surface/soil messages: {sorted(missing)}")


def _grib_field(row: Mapping[str, object]) -> tuple[object, ...] | None:
    field = tuple(row.get(k) for k in _FIELD_KEYS)
    if field[3] != 100:
        return field
    unit = row.get("typeOfLevel")
    if unit not in ("isobaricInPa", "isobaricInhPa"):
        raise ValueError("Unknown GRIB isobaric coordinate units")
    pressure_pa = _number(row, "level") * (100.0 if unit == "isobaricInhPa" else 1.0)
    # ungrib's default pmin is 100 Pa. metgrid adds one surface level.
    # https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/src/read_namelist.F
    return (*field[:4], pressure_pa, pressure_pa) if pressure_pa >= 100.0 else None


def _validate_grib_file(
    path: Path, lock: RunLock, source: ForcingProfile, domain: DomainProfile, fhr: int
) -> None:
    cycle = parse_iso_utc(lock.forcing_cycle_utc)
    valid = cycle + timedelta(hours=fhr)
    expected = (
        2,
        7,
        int(cycle.strftime("%Y%m%d")),
        int(cycle.strftime("%H%M")),
        int(valid.strftime("%Y%m%d")),
        int(valid.strftime("%H%M")),
    )
    messages = _inspect_grib(path)
    _validate_gfs_coverage(messages[0], domain, source)
    grid = messages[0].get("md5Section3")
    offset = 0
    fields: set[tuple[object, ...]] = set()
    for row in messages:
        identity = tuple(
            row.get(k)
            for k in ("edition", "centre", "dataDate", "dataTime", "validityDate", "validityTime")
        )
        if identity != expected or row.get("generatingProcessIdentifier") != (96 if fhr else 81):
            raise ValueError(f"Wrong GFS source, cycle or valid time in {path} for f{fhr:03d}")
        if not isinstance(grid, str) or not grid or row.get("md5Section3") != grid:
            raise ValueError(f"Mixed or missing GFS grids in {path}")
        length = int(_number(row, "totalLength"))
        if row.get("offset") != offset or length < 20 or row.get("packingType") == "grid_ccsds":
            raise ValueError(f"Invalid GRIB2 framing or unsupported packing in {path}")
        offset += length
        if row.get("productDefinitionTemplateNumber") == 0:
            field = _grib_field(row)
            if field is not None:
                fields.add(field)
    if offset != path.stat().st_size:
        raise ValueError(f"Incomplete GRIB2 framing in {path}")
    _validate_gfs_inventory(fields, domain, source)


def has_wps_forcing_recipe(source: ForcingProfile) -> bool:
    """Check for an implemented recipe for this provider, table and cadence."""
    return (source.provider, source.vtable_name, source.interval_seconds) == (
        "NOAA",
        "Vtable.GFS",
        3600,
    )


def validate_source_recipe(source: ForcingProfile) -> None:
    """Require an implemented forcing recipe before planning or ingesting GRIB2."""
    if not has_wps_forcing_recipe(source):
        raise ValueError(f"No validated WPS forcing recipe for {source.profile_id}")


def write_forcing_manifest(
    grib_dir: Path,
    lock: RunLock,
    source: ForcingProfile,
    files_by_fhr: Mapping[int, Path],
    *,
    domain: DomainProfile,
) -> ForcingManifest:
    """Verify source inventory and coverage, then install the immutable forcing manifest."""
    validate_source_recipe(source)
    req_fhrs = required_forcing_forecast_hours(lock, source)
    if source.profile_id != lock.forcing_profile_id or tuple(sorted(files_by_fhr)) != req_fhrs:
        raise ValueError("Staged forcing source and forecast hours must match the RunLock")
    files = tuple(files_by_fhr[fhr].resolve() for fhr in req_fhrs)
    if len(set(files)) != len(files):
        raise ValueError("Each GFS forecast hour requires its own GRIB2 file")
    file_shas = []
    for fhr, path in zip(req_fhrs, files, strict=True):
        _validate_staged_path(grib_dir.resolve(), path)
        _validate_grib_file(path, lock, source, domain, fhr)
        file_shas.append((path.name, sha256_file(path)))
    manifest = ForcingManifest(
        product_id=lock.product_id,
        cycle_partition=lock.cycle_partition,
        forcing_profile_id=source.profile_id,
        forcing_cycle_utc=lock.forcing_cycle_utc,
        vtable_name=source.vtable_name,
        interval_seconds=source.interval_seconds,
        forecast_hours=req_fhrs,
        file_sha256s=tuple(file_shas),
    )
    payload = json_bytes(manifest.to_doc())
    put_idempotent(LocalFileStore(grib_dir), FORCING_MANIFEST_FILENAME, payload)
    return manifest


def _validate_staged_path(root: Path, path: Path) -> None:
    if path.parent != root or not path.is_file() or not path.stat().st_size:
        raise ValueError(f"Invalid or empty staged forcing file: {path}")


def verify_forcing_manifest(
    grib_dir: Path, lock: RunLock, source: ForcingProfile, *, domain: DomainProfile
) -> ForcingManifest:
    """Verify manifest binding, message metadata and streaming SHA-256s for each forecast hour."""
    validate_source_recipe(source)
    m_path = grib_dir / FORCING_MANIFEST_FILENAME
    if not m_path.is_file():
        raise FileNotFoundError(
            f"Missing {FORCING_MANIFEST_FILENAME} in {grib_dir}; stage verified forcing first"
        )
    doc = json.loads(m_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"Expected JSON object in {m_path}")
    manifest = ForcingManifest.from_doc(doc)
    req_fhrs = required_forcing_forecast_hours(lock, source)
    if (
        manifest.product_id != lock.product_id
        or manifest.cycle_partition != lock.cycle_partition
        or manifest.forcing_profile_id != lock.forcing_profile_id
        or manifest.forcing_profile_id != source.profile_id
        or manifest.forcing_cycle_utc != lock.forcing_cycle_utc
        or manifest.vtable_name != source.vtable_name
        or manifest.interval_seconds != source.interval_seconds
        or manifest.forecast_hours != req_fhrs
        or len(manifest.file_sha256s) != len(req_fhrs)
    ):
        raise ValueError("forcing_manifest.json does not match frozen RunLock and ForcingProfile")
    grib_root = grib_dir.resolve()
    names = {fname for fname, _ in manifest.file_sha256s}
    if len(names) != len(req_fhrs):
        raise ValueError("Each GFS forecast hour requires its own GRIB2 file")
    for fhr, (fname, exp_sha) in zip(req_fhrs, manifest.file_sha256s, strict=True):
        fpath = (grib_root / fname).resolve()
        if fpath.name != fname:
            raise ValueError(f"Expected a canonical forcing filename, got {fname}")
        _validate_staged_path(grib_root, fpath)
        if sha256_file(fpath) != exp_sha:
            raise ValueError(f"Forcing file SHA-256 mismatch for {fpath}")
        _validate_grib_file(fpath, lock, source, domain, fhr)
    return manifest
