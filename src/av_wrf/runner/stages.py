"""Strict YAML profile loader and pipeline stage coordinators (`check` and `plan`)."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from av_wrf.codecs import build_zarr_v3_store_metadata
from av_wrf.contracts import (
    BuildManifest,
    DomainProfile,
    ForcingProfile,
    OutputProfile,
    ProductCheckSummary,
    ProductSpec,
    RunLock,
    ScienceProfile,
    parse_domain_profile,
    parse_forcing_profile,
    parse_output_profile,
    parse_product_spec,
    parse_science_profile,
)
from av_wrf.core import (
    compile_wrf_namelist_sections,
    estimate_auto_levels_agl_m,
    published_grid_origin_xy_m,
    published_grid_shape,
    resolve_cycle_window,
    selected_lead_hours,
    validate_horizon_policy,
    validate_vertical_column,
    verify_run_lock_timestamps,
)
from av_wrf.runner.store import ObjectStore

ARARAT_VALLEY_M = 800.0
MOUNT_ARAGATS_PEAK_M = 4_090.0
MOUNT_ARARAT_PEAK_M = 5_137.0


@dataclass(frozen=True)
class LoadedBundle:
    product: ProductSpec
    domain: DomainProfile
    science: ScienceProfile
    primary_source: ForcingProfile
    candidate_source: ForcingProfile
    output: OutputProfile
    config_sha256: str


def _coerce_scalar(token: str) -> str | int | float | bool:
    if (token.startswith('"') and token.endswith('"')) or (
        token.startswith("'") and token.endswith("'")
    ):
        return token[1:-1]
    if token == "true":
        return True
    if token == "false":
        return False
    try:
        return int(token)
    except ValueError:
        pass
    try:
        return float(token)
    except ValueError:
        return token


def _guard_yaml_line(path: Path, raw_line: str) -> str:
    if "\t" in raw_line:
        raise ValueError(f"{path}: tabs are forbidden in declarative YAML")
    line = raw_line.split("#", 1)[0].rstrip()
    if "${" in line or "{{" in line:
        raise ValueError(f"{path}: templating/env interpolation is forbidden: {raw_line}")
    return line


def parse_declarative_yaml(path: Path) -> dict[str, object]:
    """Parse flat YAML while rejecting duplicate keys, templating, tabs, and anchors."""
    data: dict[str, object] = {}
    active_list_key: str | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = _guard_yaml_line(path, raw_line)
        if not line.strip():
            continue
        stripped = line.lstrip()
        if stripped.startswith(("&", "*")):
            raise ValueError(f"{path}: YAML anchors/aliases are forbidden")
        if stripped.startswith("- "):
            if active_list_key is None:
                raise ValueError(f"{path}: orphan list item: {raw_line}")
            items = data[active_list_key]
            if not isinstance(items, list):
                raise ValueError(f"{path}: invalid list target: {active_list_key}")
            items.append(_coerce_scalar(stripped[2:].strip()))
            continue
        active_list_key = None
        key, sep, rest = line.partition(":")
        if not sep:
            raise ValueError(f"{path}: invalid mapping line: {raw_line}")
        k, v = key.strip(), rest.strip()
        if k in data:
            raise ValueError(f"{path}: duplicate YAML key '{k}'")
        if not v:
            data[k] = []
            active_list_key = k
        elif v.startswith("[") and v.endswith("]"):
            inner = v[1:-1].strip()
            data[k] = (
                [_coerce_scalar(part.strip()) for part in inner.split(",")] if inner else []
            )
        else:
            data[k] = _coerce_scalar(v)
    return data


def _load_bundle(config_root: Path, product_id: str) -> LoadedBundle:
    p_path = config_root / "products" / f"{product_id}.yaml"
    product = parse_product_spec(parse_declarative_yaml(p_path))
    d_path = config_root / "profiles" / "domains" / f"{product.domain_profile}.yaml"
    s_path = config_root / "profiles" / "science" / f"{product.science_profile}.yaml"
    src1_path = config_root / "profiles" / "sources" / f"{product.primary_source_profile}.yaml"
    src2_path = (
        config_root / "profiles" / "sources" / f"{product.candidate_source_profile}.yaml"
    )
    o_path = config_root / "profiles" / "outputs" / f"{product.output_profile}.yaml"

    hasher = hashlib.sha256()
    for path in (p_path, d_path, s_path, src1_path, src2_path, o_path):
        hasher.update(path.read_bytes())

    return LoadedBundle(
        product=product,
        domain=parse_domain_profile(parse_declarative_yaml(d_path)),
        science=parse_science_profile(parse_declarative_yaml(s_path)),
        primary_source=parse_forcing_profile(parse_declarative_yaml(src1_path)),
        candidate_source=parse_forcing_profile(parse_declarative_yaml(src2_path)),
        output=parse_output_profile(parse_declarative_yaml(o_path)),
        config_sha256=hasher.hexdigest(),
    )


def _validate_bundle(b: LoadedBundle) -> tuple[int, int, tuple[int, ...]]:
    remaining_floor = validate_horizon_policy(b.product, b.primary_source, b.output)
    if not b.candidate_source.fail_closed:
        raise ValueError("Candidate ForcingProfile must enforce fail_closed=True")
    bl_counts = [
        validate_vertical_column(
            estimate_auto_levels_agl_m(b.domain, elev_m, etac=b.science.etac), b.domain
        )
        for elev_m in (ARARAT_VALLEY_M, MOUNT_ARAGATS_PEAK_M, MOUNT_ARARAT_PEAK_M)
    ]
    leads = selected_lead_hours(b.output)
    return remaining_floor, min(bl_counts), leads


def check_product_config(config_root: Path, product_id: str) -> ProductCheckSummary:
    """Load, cross-reference, and validate a product, vertical stretch, and Zarr v3 store."""
    b = _load_bundle(config_root, product_id)
    remaining_floor, bl_levels, leads = _validate_bundle(b)
    namelist = compile_wrf_namelist_sections(b.domain, b.science)
    store_docs = build_zarr_v3_store_metadata(b.domain, b.output, leads)
    root_fmt = store_docs["zarr.json"]["zarr_format"]
    x0_m, y0_m = published_grid_origin_xy_m(b.domain)
    return ProductCheckSummary(
        product_id=b.product.product_id,
        published_yx=published_grid_shape(b.domain),
        x0_m=x0_m,
        y0_m=y0_m,
        primary_source=b.primary_source.profile_id,
        candidate_source=b.candidate_source.profile_id,
        lead_count=len(leads),
        bl_levels_2km=bl_levels,
        remaining_horizon_floor_hours=remaining_floor,
        time_step_s=int(namelist["domains"]["time_step"]),
        zarr_format=int(root_fmt) if isinstance(root_fmt, int) else 3,
    )


def plan_product_cycle(
    config_root: Path,
    store: ObjectStore,
    *,
    product_id: str,
    publish_origin_utc: str,
    attempt_id: str,
    mpi_ranks: int,
    build_manifest: BuildManifest,
    geog_bundle_sha256: str = "pending-geog-preflight",
) -> tuple[str, RunLock]:
    """Resolve a cycle into an immutable RunLock and persist run.lock.json exclusively."""
    if not attempt_id.strip():
        raise ValueError("attempt_id must be non-empty")
    b = _load_bundle(config_root, product_id)
    _validate_bundle(b)
    compile_wrf_namelist_sections(b.domain, b.science)
    window = resolve_cycle_window(b.product, b.output, publish_origin_utc)
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
        geog_bundle_sha256=geog_bundle_sha256,
        build_manifest=build_manifest,
        mpi_ranks=mpi_ranks,
    )
    verify_run_lock_timestamps(lock)
    key = (
        f"runs/{lock.product_id}/{lock.cycle_partition}"
        f"/attempts/{lock.attempt_id}/run.lock.json"
    )
    payload = (json.dumps(dataclasses.asdict(lock), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    store.put_if_absent(key, payload)
    return key, lock
