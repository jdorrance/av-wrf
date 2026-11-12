"""Declarative PyYAML profile loader, SHA-256 bundle hasher, and cross-profile validator."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from av_wrf.codecs import build_zarr_v3_store_metadata
from av_wrf.contracts import (
    DomainProfile,
    ForcingProfile,
    OutputProfile,
    ProductSpec,
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
    resolve_cycle_window,
    validate_horizon_policy,
    validate_vertical_column,
)
from av_wrf.runner.forcing_recipe import ForcingRecipe
from av_wrf.runner.mirrors import ForcingSubset, forcing_subset, resolve_forcing_window


@dataclass(frozen=True)
class LoadedBundle:
    product: ProductSpec
    domain: DomainProfile
    science: ScienceProfile
    primary_source: ForcingProfile
    candidate_source: ForcingProfile
    output: OutputProfile
    config_sha256: str


class _StrictYamlLoader(yaml.SafeLoader):
    """SafeLoader rejecting duplicate YAML mapping keys."""

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
        seen: set[object] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in seen:
                raise ValueError(f"Duplicate YAML key '{key}' is forbidden")
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


def parse_declarative_yaml(path: Path) -> dict[str, object]:
    """Parse declarative YAML via PyYAML while forbidding tabs, templating, anchors, and dupes."""
    return _parse_yaml_text(path, path.read_text(encoding="utf-8"))


def _parse_yaml_text(path: Path, raw_text: str) -> dict[str, object]:
    for lineno, raw_line in enumerate(raw_text.splitlines(), start=1):
        if "\t" in raw_line:
            raise ValueError(f"{path}:{lineno}: tabs are forbidden in declarative YAML")
        clean = raw_line.split("#", 1)[0]
        if "${" in clean or "{{" in clean:
            raise ValueError(f"{path}:{lineno}: templating/env interpolation is forbidden")
    try:
        for token in yaml.scan(raw_text, Loader=_StrictYamlLoader):
            if isinstance(token, (yaml.AnchorToken, yaml.AliasToken)):
                raise ValueError(f"{path}: YAML anchors/aliases are forbidden")
        loaded = yaml.load(raw_text, Loader=_StrictYamlLoader)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML syntax: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ValueError(f"{path}: expected top-level YAML mapping")
    out: dict[str, object] = {}
    for k, v in loaded.items():
        if not isinstance(k, str) or isinstance(v, dict):
            raise ValueError(f"{path}: expected flat string-keyed profile entry for '{k}'")
        out[k] = v
    return out


def require_path_segment(value: str, label: str) -> None:
    """Require a single component for a profile filename or object key."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValueError(f"{label} must be a single safe path segment")


def load_bundle(config_root: Path, product_id: str) -> LoadedBundle:
    """Load and hash the same bytes for the product and each referenced profile."""
    hasher = hashlib.sha256()

    def read(folder: str, ident: str, key: str = "profile_id") -> dict[str, object]:
        require_path_segment(ident, key)
        path = config_root / folder / f"{ident}.yaml"
        data = path.read_bytes()
        hasher.update(data)
        doc = _parse_yaml_text(path, data.decode("utf-8"))
        if doc.get(key) != ident:
            raise ValueError(f"{path}: {key} must match the filename")
        return doc

    product = parse_product_spec(read("products", product_id, "product_id"))
    require_path_segment(product.model_id, "model_id")
    return LoadedBundle(
        product=product,
        domain=parse_domain_profile(read("profiles/domains", product.domain_profile)),
        science=parse_science_profile(read("profiles/science", product.science_profile)),
        primary_source=parse_forcing_profile(
            read("profiles/sources", product.primary_source_profile)
        ),
        candidate_source=parse_forcing_profile(
            read("profiles/sources", product.candidate_source_profile)
        ),
        output=parse_output_profile(read("profiles/outputs", product.output_profile)),
        config_sha256=hasher.hexdigest(),
    )


def load_forcing_plan(
    path: Path, config_root: Path, expected_recipe: ForcingRecipe
) -> tuple[LoadedBundle, datetime, ForcingSubset]:
    """Verify the prep plan against the configured source, footprint, cycle, and file keys."""
    doc = json.loads(path.read_bytes())
    if not isinstance(doc, dict):
        raise ValueError("Expected a forcing_subset.json object")
    product, partition = doc.get("product_id"), doc.get("cycle_partition")
    if not isinstance(product, str) or not isinstance(partition, str):
        raise ValueError("Forcing plan requires product_id and cycle_partition")
    bundle = load_bundle(config_root, product)
    window = resolve_cycle_window(bundle.product, bundle.output, partition)
    recipe, cycle, expected = forcing_subset(product, window, bundle.primary_source, bundle.domain)
    if recipe is not expected_recipe or any(
        doc.get(key) != value for key, value in expected.items()
    ):
        raise ValueError("Forcing plan differs from the configured source inventory")
    return bundle, cycle, expected


def validate_bundle(
    b: LoadedBundle, *, publish_origin_utc: str | None = None
) -> tuple[int, int, tuple[int, ...]]:
    """Validate horizon SLA, candidate source, vertical BL stretch, and Zarr v3 store schema."""
    origins: tuple[str, ...]
    if publish_origin_utc is None:
        year, hour = datetime.now(UTC).year, b.product.publish_origin_hour_utc
        origins = (f"{year:04d}-01-01T{hour:02d}:00:00Z", f"{year:04d}-07-01T{hour:02d}:00:00Z")
    else:
        origins = (publish_origin_utc,)
    window = resolve_cycle_window(b.product, b.output, origins[0])
    resolve_forcing_window(window, b.primary_source, domain=b.domain)
    remaining_floor = min(
        validate_horizon_policy(b.product, b.primary_source, b.output, publish_origin_utc=origin)
        for origin in (window.publish_origin_utc, *origins[1:])
    )
    compile_wrf_namelist_sections(b.domain, b.science, b.primary_source)
    if not b.candidate_source.fail_closed:
        raise ValueError("Candidate ForcingProfile must enforce fail_closed=True")
    bl_counts = [
        validate_vertical_column(
            estimate_auto_levels_agl_m(
                b.domain,
                elev_m,
                etac=b.science.etac,
                base_temp_k=b.science.base_temp_k,
            ),
            b.domain,
        )
        for elev_m in b.domain.terrain_elevations_m
    ]
    leads = window.lead_hours
    build_zarr_v3_store_metadata(b.domain, b.output, leads)
    return remaining_floor, min(bl_counts), leads


__all__ = [
    "LoadedBundle",
    "load_bundle",
    "load_forcing_plan",
    "parse_declarative_yaml",
    "require_path_segment",
    "validate_bundle",
]
