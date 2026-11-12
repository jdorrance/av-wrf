"""Validate staged forcing GRIB2 files against their source recipe and RunLock."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from av_wrf.contracts import (
    CycleWindow,
    DomainProfile,
    ForcingManifest,
    ForcingProfile,
    RunLock,
    parse_iso_utc,
)
from av_wrf.core import computational_latlon_bounds, forcing_domain, required_forcing_forecast_hours
from av_wrf.runner.forcing_gfs import GFS
from av_wrf.runner.forcing_icon import ICON_EU
from av_wrf.runner.forcing_ifs import IFS
from av_wrf.runner.forcing_recipe import ForcingRecipe
from av_wrf.runner.store import (
    LocalFileStore,
    _copy_sha256_file,
    json_bytes,
    put_idempotent,
    sha256_file,
    sha256_hex,
)

FORCING_MANIFEST_FILENAME = "forcing_manifest.json"


_RECIPES = {recipe.identity: recipe for recipe in (GFS, ICON_EU, IFS)}


class ForcingSubset(TypedDict):
    product_id: str
    cycle_partition: str
    forcing_profile_id: str
    forcing_cycle_utc: str
    vtable_name: str
    mirrors: list[str]
    interval_seconds: int
    bbox_wsen_deg: list[float]
    forecast_hours: list[int]
    file_keys: dict[str, str | list[str]]


def find_forcing_recipe(source: ForcingProfile) -> ForcingRecipe | None:
    """Find a WPS recipe matching the declared source product."""
    return _RECIPES.get(
        (source.provider, source.vtable_name, source.interval_seconds, source.grid_spacing_deg)
    )


def resolve_forcing_window(
    window: RunLock | CycleWindow, source: ForcingProfile, *, domain: DomainProfile
) -> tuple[ForcingRecipe, datetime, tuple[int, ...]]:
    """Resolve the recipe, UTC cycle, and available WPS boundary forecast hours."""
    if isinstance(window, RunLock) and source.profile_id != window.forcing_profile_id:
        raise ValueError("Staged forcing source must match the RunLock")
    cycle = parse_iso_utc(window.forcing_cycle_utc)
    if cycle.minute or cycle.second or cycle.microsecond:
        raise ValueError("Forcing cycle must start on a whole UTC hour")
    recipe = find_forcing_recipe(source)
    if recipe is None:
        raise ValueError(f"No validated WPS forcing recipe for {source.profile_id}")
    hours = required_forcing_forecast_hours(window, source)
    recipe.check_window(source, domain, cycle, hours)
    return recipe, cycle, hours


def forcing_subset(
    product_id: str, window: RunLock | CycleWindow, source: ForcingProfile, domain: DomainProfile
) -> tuple[ForcingRecipe, datetime, ForcingSubset]:
    """Build the prep document for the resolved source and boundary cycle."""
    recipe, cycle, hours = resolve_forcing_window(window, source, domain=domain)
    return (
        recipe,
        cycle,
        {
            "product_id": product_id,
            "cycle_partition": window.cycle_partition,
            "forcing_profile_id": source.profile_id,
            "forcing_cycle_utc": window.forcing_cycle_utc,
            "vtable_name": source.vtable_name,
            "mirrors": list(source.mirrors),
            "interval_seconds": source.interval_seconds,
            "bbox_wsen_deg": list(
                computational_latlon_bounds(
                    forcing_domain(domain, source.grid_spacing_deg),
                    snap_deg=source.grid_spacing_deg,
                )
            ),
            "forecast_hours": list(hours),
            "file_keys": {
                hour: keys if isinstance(keys, str) else list(keys)
                for hour, keys in recipe.file_keys(cycle, hours).items()
            },
        },
    )


def _forcing_manifest(
    lock: RunLock,
    source: ForcingProfile,
    hours: tuple[int, ...],
    file_shas: tuple[tuple[str, str], ...],
    acquisition_inventory: tuple[str, str] | None,
) -> ForcingManifest:
    return ForcingManifest(
        product_id=lock.product_id,
        cycle_partition=lock.cycle_partition,
        forcing_profile_id=source.profile_id,
        forcing_cycle_utc=lock.forcing_cycle_utc,
        vtable_name=source.vtable_name,
        interval_seconds=source.interval_seconds,
        forecast_hours=hours,
        file_sha256s=file_shas,
        acquisition_inventory=acquisition_inventory,
    )


def _bind_acquisition_inventory(
    path: Path,
    root: Path,
    cycle: datetime,
    groups: Mapping[str, str | tuple[str, ...]],
    file_shas: list[tuple[str, str]],
) -> tuple[str, str]:
    path = path.resolve()
    _validate_staged_path(root, path)
    raw = path.read_bytes()
    doc = json.loads(raw)
    if not isinstance(doc, dict) or doc.get("normalized_sha256s") != dict(file_shas):
        raise ValueError("Acquisition inventory does not match normalized forcing files")
    source_shas = doc.get("source_sha256s")
    expected = {
        key for group in groups.values() for key in ((group,) if isinstance(group, str) else group)
    }
    if (
        parse_iso_utc(str(doc.get("forcing_cycle_utc"))) != cycle
        or not isinstance(source_shas, dict)
        or source_shas.keys() != expected
        or any(
            not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha)
            for sha in source_shas.values()
        )
    ):
        raise ValueError("Acquisition inventory does not match the forcing cycle or source keys")
    return path.name, sha256_hex(raw)


def write_forcing_manifest(
    grib_dir: Path,
    lock: RunLock,
    source: ForcingProfile,
    files_by_fhr: Mapping[int, Path],
    *,
    domain: DomainProfile,
    acquisition_inventory: Path | None = None,
) -> ForcingManifest:
    """Verify source inventory and coverage, then install the immutable forcing manifest."""
    recipe, cycle, req_fhrs = resolve_forcing_window(lock, source, domain=domain)
    if tuple(sorted(files_by_fhr)) != req_fhrs:
        raise ValueError("Staged forcing forecast hours must match the RunLock")
    root = grib_dir.resolve()
    files = tuple(files_by_fhr[fhr].resolve() for fhr in req_fhrs)
    if len(set(files)) != len(files):
        raise ValueError("Each forecast hour requires its own forcing file")
    file_shas = []
    for fhr, path in zip(req_fhrs, files, strict=True):
        _validate_staged_path(root, path)
        recipe.validate_file(path, cycle, source, domain, fhr)
        file_shas.append((path.name, sha256_file(path)))
    inventory = (
        None
        if acquisition_inventory is None
        else _bind_acquisition_inventory(
            acquisition_inventory, root, cycle, recipe.file_keys(cycle, req_fhrs), file_shas
        )
    )
    manifest = _forcing_manifest(lock, source, req_fhrs, tuple(file_shas), inventory)
    payload = json_bytes(manifest.to_doc())
    put_idempotent(LocalFileStore(grib_dir), FORCING_MANIFEST_FILENAME, payload)
    return manifest


def _validate_staged_path(root: Path, path: Path) -> None:
    if path.parent != root or not path.is_file() or not path.stat().st_size:
        raise ValueError(f"Invalid or empty staged forcing file: {path}")


def verify_forcing_manifest(
    grib_dir: Path,
    lock: RunLock,
    source: ForcingProfile,
    *,
    domain: DomainProfile,
    snapshot_dir: Path | None = None,
) -> ForcingManifest:
    """Verify forcing provenance and metadata, using private copies when requested."""
    recipe, cycle, req_fhrs = resolve_forcing_window(lock, source, domain=domain)
    m_path = grib_dir / FORCING_MANIFEST_FILENAME
    if not m_path.is_file():
        raise FileNotFoundError(
            f"Missing {FORCING_MANIFEST_FILENAME} in {grib_dir}; stage verified forcing first"
        )
    doc = json.loads(m_path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        raise ValueError(f"Expected JSON object in {m_path}")
    manifest = ForcingManifest.from_doc(doc)
    inventory = manifest.acquisition_inventory
    expected = _forcing_manifest(lock, source, req_fhrs, manifest.file_sha256s, inventory)
    if manifest != expected or len(manifest.file_sha256s) != len(req_fhrs):
        raise ValueError("forcing_manifest.json does not match frozen RunLock and ForcingProfile")
    grib_root = grib_dir.resolve()
    if inventory is not None:
        name, digest = inventory
        path = (grib_root / name).resolve()
        _validate_staged_path(grib_root, path)
        if path.name != name or sha256_file(path) != digest:
            raise ValueError("Acquisition inventory SHA-256 mismatch")
    names = {fname for fname, _ in manifest.file_sha256s}
    if len(names) != len(req_fhrs):
        raise ValueError("Each forecast hour requires its own forcing file")
    for fhr, (fname, exp_sha) in zip(req_fhrs, manifest.file_sha256s, strict=True):
        fpath = (grib_root / fname).resolve()
        if fpath.name != fname:
            raise ValueError(f"Expected a canonical forcing filename, got {fname}")
        _validate_staged_path(grib_root, fpath)
        staged = fpath if snapshot_dir is None else snapshot_dir / fname
        digest = sha256_file(fpath) if snapshot_dir is None else _copy_sha256_file(fpath, staged)
        if digest != exp_sha:
            raise ValueError(f"Forcing file SHA-256 mismatch for {fpath}")
        recipe.validate_file(staged, cycle, source, domain, fhr)
    return manifest


__all__ = [
    "FORCING_MANIFEST_FILENAME",
    "ForcingSubset",
    "find_forcing_recipe",
    "forcing_subset",
    "resolve_forcing_window",
    "verify_forcing_manifest",
    "write_forcing_manifest",
]
