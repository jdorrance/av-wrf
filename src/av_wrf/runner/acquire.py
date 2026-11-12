"""Acquire and seal the forcing inventory for a frozen cycle."""

from __future__ import annotations

import json
from pathlib import Path

import httpx

from av_wrf.contracts import ForcingManifest, RunLock
from av_wrf.runner.bundle import LoadedBundle
from av_wrf.runner.forcing_gfs import GFS
from av_wrf.runner.forcing_icon import ICON_EU
from av_wrf.runner.forcing_ifs import IFS
from av_wrf.runner.gfs_acquire import _acquire
from av_wrf.runner.icon_acquire import _assemble, _icon_plan
from av_wrf.runner.ifs_acquire import acquire_ifs
from av_wrf.runner.mirrors import forcing_subset, write_forcing_manifest


def acquire_forcing(
    bundle: LoadedBundle,
    lock: RunLock,
    destination: Path,
    *,
    plan_path: Path | None = None,
    cache: Path | None = None,
    workers: int = 8,
    gfs_regional_filter: bool = False,
) -> ForcingManifest:
    """Acquire every locked boundary and seal its source and normalized hashes."""
    if workers < 1 or workers > 64:
        raise ValueError("workers must be between 1 and 64")
    recipe, cycle, plan = forcing_subset(
        bundle.product.product_id, lock, bundle.primary_source, bundle.domain
    )
    if gfs_regional_filter and recipe is not GFS:
        raise ValueError("The regional NOAA filter requires a GFS source")
    if plan_path is not None:
        doc = json.loads(plan_path.read_bytes())
        if not isinstance(doc, dict) or any(doc.get(key) != value for key, value in plan.items()):
            raise ValueError("Forcing plan differs from the locked source inventory")
    if recipe is GFS:
        with httpx.Client(timeout=120) as client:
            files = _acquire(
                client,
                bundle,
                cycle,
                plan,
                destination,
                None,
                mode="regional" if gfs_regional_filter else "ranges",
            )
        prefix = f"gfs.{cycle:%Y%m%d%H}"
    elif recipe is ICON_EU:
        if cache is None:
            raise ValueError("ICON acquisition requires a source object cache")
        bounds, groups = _icon_plan(plan)
        files = _assemble(bundle, cycle, bounds, groups, cache, destination, workers)
        prefix = f"icon-eu.{cycle:%Y%m%d%H}"
    elif recipe is IFS:
        with httpx.Client(timeout=120) as client:
            files = acquire_ifs(client, bundle, cycle, plan, destination)
        prefix = f"ifs.{cycle:%Y%m%d%H}"
    else:
        raise ValueError("No acquisition implementation for the locked source")
    inventory = destination / f"{prefix}.sources-f{max(files):03d}.json"
    return write_forcing_manifest(
        destination,
        lock,
        bundle.primary_source,
        {hour: destination / name for hour, name in files.items()},
        domain=bundle.domain,
        acquisition_inventory=inventory,
    )


__all__ = ["acquire_forcing"]
