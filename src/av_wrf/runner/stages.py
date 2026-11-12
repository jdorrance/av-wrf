"""Pipeline stage coordinators (`check`, `plan`, `load_run_lock`, and `prep`)."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from math import isqrt
from pathlib import Path

from av_wrf.contracts import (
    ZARR_FORMAT_VERSION,
    BuildManifest,
    CycleWindow,
    ProductCheckSummary,
    RunLock,
)
from av_wrf.core import (
    compile_wps_namelist_sections,
    compile_wrf_run_namelist,
    published_grid_origin_xy_m,
    published_grid_shape,
    render_fortran_namelist,
    resolve_cycle_window,
)
from av_wrf.runner.bundle import LoadedBundle, load_bundle, require_path_segment, validate_bundle
from av_wrf.runner.mirrors import find_forcing_recipe, forcing_subset
from av_wrf.runner.store import ObjectStore, json_bytes, run_lock_key, sha256_file


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
        candidate_recipe_available=find_forcing_recipe(b.candidate_source) is not None,
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
    """Persist an immutable RunLock and accept an identical retry."""
    require_path_segment(attempt_id, "attempt_id")
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    window = resolve_cycle_window(b.product, b.output, publish_origin_utc)
    validate_bundle(b, publish_origin_utc=window.publish_origin_utc)
    lock = _build_run_lock(
        b,
        window,
        attempt_id,
        mpi_ranks,
        build_manifest,
        geog_bundle_sha256 or "host-dev-unpinned",
        _producer_sha256(),
    )
    key = run_lock_key(lock.product_id, lock.cycle_partition, lock.attempt_id)
    try:
        store.put_if_absent(key, json_bytes(lock.to_doc()))
    except FileExistsError:
        stored = load_run_lock(store, lock.product_id, lock.cycle_partition, attempt_id, bundle=b)
        if stored not in (lock, replace(lock, producer_sha256=None)):
            raise
        return key, stored
    return key, lock


def _producer_sha256() -> str:
    package = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(package.rglob("*.py")):
        digest.update(path.relative_to(package).as_posix().encode("utf-8") + b"\0")
        digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()


def verify_producer_sha256(lock: RunLock) -> None:
    """Verify the producer source tree against the recorded run-lock digest."""
    if lock.producer_sha256 is not None and lock.producer_sha256 != _producer_sha256():
        raise ValueError("Producer code SHA-256 drifted from frozen RunLock")


def _build_run_lock(
    b: LoadedBundle,
    window: CycleWindow,
    attempt_id: str,
    mpi_ranks: int,
    build_manifest: BuildManifest,
    geog_bundle_sha256: str,
    producer_sha256: str | None,
) -> RunLock:
    if isinstance(mpi_ranks, bool) or mpi_ranks < 1:
        raise ValueError("RunLock mpi_ranks must be >= 1")
    # WRF chooses the closest factor pair, with the smaller factor in X.
    # https://github.com/wrf-model/WRF/blob/v4.8.0/external/RSL_LITE/module_dm.F#L90-L161
    # https://github.com/wrf-model/WRF/blob/v4.8.0/share/module_check_a_mundo.F#L2773-L2799
    max_x, max_y = b.domain.e_we // 10, b.domain.e_sn // 10
    if mpi_ranks > max_x * max_y:
        raise ValueError("RunLock mpi_ranks exceeds WRF's 10-cell minimum patch width")
    ranks_x = next(x for x in range(isqrt(mpi_ranks), 0, -1) if mpi_ranks % x == 0)
    if ranks_x > max_x or mpi_ranks // ranks_x > max_y:
        raise ValueError("RunLock mpi_ranks creates a WRF patch narrower than 10 cells")
    if build_manifest.oci_digest != "host-dev-unpinned" and (
        not re.fullmatch(r"sha256:[0-9a-f]{64}", build_manifest.oci_digest)
        or not re.fullmatch(r"[0-9a-f]{64}", geog_bundle_sha256)
    ):
        raise ValueError("Live RunLock requires a pinned OCI digest and --geog-file")
    if producer_sha256 is not None and not re.fullmatch(r"[0-9a-f]{64}", producer_sha256):
        raise ValueError("RunLock producer_sha256 must be a SHA-256 hex digest")
    return RunLock(
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
        geog_bundle_sha256=geog_bundle_sha256,
        build_manifest=build_manifest,
        mpi_ranks=mpi_ranks,
        producer_sha256=producer_sha256,
    )


def load_run_lock(
    store: ObjectStore,
    product_id: str,
    cycle_partition: str,
    attempt_id: str,
    *,
    bundle: LoadedBundle,
) -> RunLock:
    """Read a RunLock and verify its attempt key and configured cycle binding."""
    require_path_segment(product_id, "product_id")
    require_path_segment(attempt_id, "attempt_id")
    if not re.fullmatch(r"[0-9]{8}/[0-9]{2}", cycle_partition):
        raise ValueError("cycle_partition must be YYYYMMDD/HH")
    raw_bytes = store.get_bytes(run_lock_key(product_id, cycle_partition, attempt_id))
    if raw_bytes is None:
        raise FileNotFoundError(
            f"Missing run.lock.json for {product_id} {cycle_partition} ({attempt_id})"
        )
    doc = json.loads(raw_bytes)
    if not isinstance(doc, dict):
        raise ValueError("Expected JSON object in run.lock.json")
    lock = RunLock.from_doc(doc)
    if (lock.product_id, lock.cycle_partition, lock.attempt_id) != (
        product_id,
        cycle_partition,
        attempt_id,
    ):
        raise ValueError("RunLock identity disagrees with its object key")
    if lock.config_sha256 != bundle.config_sha256:
        raise ValueError("Config bundle SHA-256 drifted from frozen RunLock")
    window = resolve_cycle_window(bundle.product, bundle.output, cycle_partition)
    expected = _build_run_lock(
        bundle,
        window,
        attempt_id,
        lock.mpi_ranks,
        lock.build_manifest,
        lock.geog_bundle_sha256,
        lock.producer_sha256,
    )
    if lock != expected:
        raise ValueError("RunLock disagrees with configured product, profiles, or cycle window")
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
) -> tuple[Path, Path]:
    """Verify RunLock and write WPS, real, WRF, and forcing subset inputs."""
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    lk = load_run_lock(store, product_id, cycle_partition, attempt_id, bundle=b)
    source = b.primary_source
    _, _, subset = forcing_subset(lk.product_id, lk, source, b.domain)
    work_dir.mkdir(parents=True, exist_ok=True)
    wps_path, wrf_path = work_dir / "namelist.wps", work_dir / "namelist.input"
    wps_path.write_text(
        render_fortran_namelist(compile_wps_namelist_sections(b.domain, b.science, lk, source)),
        encoding="utf-8",
    )
    wrf_path.write_text(
        render_fortran_namelist(compile_wrf_run_namelist(b.domain, b.science, lk, source)),
        encoding="utf-8",
    )
    (work_dir / "namelist.input.real").write_text(
        render_fortran_namelist(
            compile_wrf_run_namelist(b.domain, b.science, lk, source, for_real=True)
        ),
        encoding="utf-8",
    )
    (work_dir / "forcing_subset.json").write_bytes(json_bytes(subset))
    return wps_path, wrf_path


__all__ = [
    "check_all_products",
    "check_product_config",
    "load_run_lock",
    "plan_product_cycle",
    "prep_cycle_namelists",
    "verify_producer_sha256",
]
