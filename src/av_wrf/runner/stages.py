"""Pipeline stage coordinators (`check`, `plan`, `load_run_lock`, and `prep`)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from av_wrf.contracts import (
    ZARR_FORMAT_VERSION,
    BuildManifest,
    ProductCheckSummary,
    RunLock,
    parse_iso_utc,
)
from av_wrf.core import (
    compile_wps_namelist_sections,
    compile_wrf_run_namelist,
    computational_latlon_bounds,
    parse_cycle_arg,
    published_grid_origin_xy_m,
    published_grid_shape,
    render_fortran_namelist,
    required_forcing_forecast_hours,
    resolve_cycle_window,
    verify_run_lock_timestamps,
)
from av_wrf.runner.bundle import LoadedBundle, _require_segment, load_bundle, validate_bundle
from av_wrf.runner.mirrors import has_wps_forcing_recipe
from av_wrf.runner.store import ObjectStore, json_bytes, run_lock_key


def check_product_config(config_root: Path, product_id: str) -> ProductCheckSummary:
    """Load, cross-reference, and validate a product, vertical stretch, and Zarr v3 store."""
    return _check_bundle(load_bundle(config_root, product_id))


def _check_bundle(b: LoadedBundle) -> ProductCheckSummary:
    remaining_floor, bl_levels, leads = validate_bundle(b)
    x0_m, y0_m = published_grid_origin_xy_m(b.domain)
    return ProductCheckSummary(
        product_id=b.product.product_id,
        published_yx=published_grid_shape(b.domain),
        x0_m=x0_m,
        y0_m=y0_m,
        primary_source=b.primary_source.profile_id,
        candidate_source=b.candidate_source.profile_id,
        candidate_recipe_available=has_wps_forcing_recipe(b.candidate_source),
        lead_count=len(leads),
        bl_levels_2km=bl_levels,
        remaining_horizon_floor_hours=remaining_floor,
        time_step_s=b.science.time_step_s,
        zarr_format=ZARR_FORMAT_VERSION,
    )


def check_all_products(config_root: Path) -> tuple[ProductCheckSummary, ...]:
    """Discover and validate all configured product YAMLs under config_root/products."""
    product_files = sorted((config_root / "products").glob("*.yaml"))
    if not product_files:
        raise FileNotFoundError(f"No product YAML files found under {config_root / 'products'}")
    bundles = [load_bundle(config_root, path.stem) for path in product_files]
    if len({b.product.model_id for b in bundles}) != len(bundles):
        raise ValueError("Configured products must have distinct model_id publication namespaces")
    return tuple(_check_bundle(b) for b in bundles)


def resolve_cli_cycle(
    config_root: Path,
    product_id: str,
    raw_cycle: str,
    *,
    bundle: LoadedBundle | None = None,
) -> tuple[str, str]:
    """Resolve raw CLI --cycle string into (publish_origin_utc, cycle_partition)."""
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    return parse_cycle_arg(raw_cycle, b.product)


def plan_product_cycle(
    config_root: Path,
    store: ObjectStore,
    *,
    product_id: str,
    publish_origin_utc: str,
    attempt_id: str,
    mpi_ranks: int,
    build_manifest: BuildManifest,
    geog_bundle_sha256: str | None = None,
    bundle: LoadedBundle | None = None,
) -> tuple[str, RunLock]:
    """Resolve a cycle into an immutable RunLock and persist run.lock.json exclusively."""
    _require_segment(attempt_id, "attempt_id")
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    window = resolve_cycle_window(b.product, b.output, publish_origin_utc)
    validate_bundle(b, publish_origin_utc=window.publish_origin_utc)
    if build_manifest.oci_digest != "host-dev-unpinned" and (
        not re.fullmatch(r"sha256:[0-9a-f]{64}", build_manifest.oci_digest)
        or not re.fullmatch(r"[0-9a-f]{64}", geog_bundle_sha256 or "")
    ):
        raise ValueError("Live plan requires a pinned OCI digest and --geog-file")
    lock = RunLock(
        product_id=b.product.product_id,
        model_id=b.product.model_id,
        cycle_partition=window.cycle_partition,
        attempt_id=attempt_id,
        forcing_cycle_utc=window.forcing_cycle_utc,
        wrf_init_utc=window.wrf_init_utc,
        publish_origin_utc=window.publish_origin_utc,
        spinup_hours=b.product.spinup_hours,
        lead_hours=window.lead_hours,
        forcing_profile_id=b.primary_source.profile_id,
        domain_profile_id=b.domain.profile_id,
        science_profile_id=b.science.profile_id,
        output_profile_id=b.output.profile_id,
        config_sha256=b.config_sha256,
        geog_bundle_sha256=geog_bundle_sha256 or "host-dev-unpinned",
        build_manifest=build_manifest,
        mpi_ranks=mpi_ranks,
    )
    verify_run_lock_timestamps(lock)
    required_forcing_forecast_hours(lock, b.primary_source)
    key = run_lock_key(lock.product_id, lock.cycle_partition, lock.attempt_id)
    store.put_if_absent(key, json_bytes(lock.to_doc()))
    return key, lock


def load_run_lock(
    store: ObjectStore, product_id: str, cycle_partition: str, attempt_id: str
) -> RunLock:
    """Read and deserialize an immutable RunLock from ObjectStore."""
    _require_segment(product_id, "product_id")
    _require_segment(attempt_id, "attempt_id")
    if not re.fullmatch(r"[0-9]{8}/[0-9]{2}", cycle_partition):
        raise ValueError("cycle_partition must be YYYYMMDD/HH")
    raw_bytes = store.get_bytes(run_lock_key(product_id, cycle_partition, attempt_id))
    if raw_bytes is None:
        raise FileNotFoundError(
            f"Missing run.lock.json for {product_id} {cycle_partition} ({attempt_id})"
        )
    doc = json.loads(raw_bytes.decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("Expected JSON object in run.lock.json")
    lock = RunLock.from_doc(doc)
    if (lock.product_id, lock.cycle_partition, lock.attempt_id) != (
        product_id,
        cycle_partition,
        attempt_id,
    ):
        raise ValueError("RunLock identity disagrees with its object key")
    _require_segment(lock.model_id, "model_id")
    if parse_iso_utc(lock.publish_origin_utc).strftime("%Y%m%d/%H") != cycle_partition:
        raise ValueError("RunLock publish origin disagrees with its cycle partition")
    verify_run_lock_timestamps(lock)
    return lock


def prep_cycle_namelists(
    config_root: Path,
    store: ObjectStore,
    *,
    product_id: str,
    cycle_partition: str,
    attempt_id: str,
    work_dir: Path,
    bundle: LoadedBundle | None = None,
    lock: RunLock | None = None,
) -> tuple[Path, Path]:
    """Verify RunLock and write namelist.wps, namelist.input, and forcing_subset.json."""
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    lk = load_run_lock(store, product_id, cycle_partition, attempt_id) if lock is None else lock
    if lk.config_sha256 != b.config_sha256:
        raise ValueError("Config bundle SHA-256 drifted from frozen RunLock")
    work_dir.mkdir(parents=True, exist_ok=True)
    wps_path, wrf_path = work_dir / "namelist.wps", work_dir / "namelist.input"
    wps_path.write_text(
        render_fortran_namelist(
            compile_wps_namelist_sections(b.domain, b.science, lk, b.primary_source)
        ),
        encoding="utf-8",
    )
    wrf_path.write_text(
        render_fortran_namelist(
            compile_wrf_run_namelist(b.domain, b.science, lk, b.primary_source)
        ),
        encoding="utf-8",
    )
    bbox = list(computational_latlon_bounds(b.domain, snap_deg=b.primary_source.grid_spacing_deg))
    fhrs = list(required_forcing_forecast_hours(lk, b.primary_source))
    subset = {
        "product_id": lk.product_id,
        "cycle_partition": lk.cycle_partition,
        "forcing_profile_id": lk.forcing_profile_id,
        "forcing_cycle_utc": lk.forcing_cycle_utc,
        "vtable_name": b.primary_source.vtable_name,
        "mirrors": list(b.primary_source.mirrors),
        "interval_seconds": b.primary_source.interval_seconds,
        "bbox_wsen_deg": bbox,
        "forecast_hours": fhrs,
    }
    (work_dir / "forcing_subset.json").write_bytes(json_bytes(subset))
    return wps_path, wrf_path
