"""Validate staged forcing GRIB2 files against their source recipe and RunLock."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from av_wrf.contracts import (
    CycleWindow,
    DomainProfile,
    ForcingManifest,
    ForcingProfile,
    RunLock,
    parse_iso_utc,
)
from av_wrf.core import required_forcing_forecast_hours
from av_wrf.runner.forcing_gfs import GFS
from av_wrf.runner.forcing_recipe import ForcingRecipe
from av_wrf.runner.store import LocalFileStore, json_bytes, put_idempotent, sha256_file

FORCING_MANIFEST_FILENAME = "forcing_manifest.json"


_RECIPES = {recipe.identity: recipe for recipe in (GFS,)}


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


def _forcing_manifest(
    lock: RunLock,
    source: ForcingProfile,
    hours: tuple[int, ...],
    file_shas: tuple[tuple[str, str], ...],
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
    )


def write_forcing_manifest(
    grib_dir: Path,
    lock: RunLock,
    source: ForcingProfile,
    files_by_fhr: Mapping[int, Path],
    *,
    domain: DomainProfile,
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
    manifest = _forcing_manifest(lock, source, req_fhrs, tuple(file_shas))
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
    expected = _forcing_manifest(lock, source, req_fhrs, manifest.file_sha256s)
    if manifest != expected or len(manifest.file_sha256s) != len(req_fhrs):
        raise ValueError("forcing_manifest.json does not match frozen RunLock and ForcingProfile")
    grib_root = grib_dir.resolve()
    names = {fname for fname, _ in manifest.file_sha256s}
    if len(names) != len(req_fhrs):
        raise ValueError("Each forecast hour requires its own forcing file")
    for fhr, (fname, exp_sha) in zip(req_fhrs, manifest.file_sha256s, strict=True):
        fpath = (grib_root / fname).resolve()
        if fpath.name != fname:
            raise ValueError(f"Expected a canonical forcing filename, got {fname}")
        _validate_staged_path(grib_root, fpath)
        if sha256_file(fpath) != exp_sha:
            raise ValueError(f"Forcing file SHA-256 mismatch for {fpath}")
        recipe.validate_file(fpath, cycle, source, domain, fhr)
    return manifest
