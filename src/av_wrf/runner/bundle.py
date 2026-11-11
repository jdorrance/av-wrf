"""Declarative PyYAML profile loader, SHA-256 bundle hasher, and cross-profile validator."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import yaml

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
    estimate_auto_levels_agl_m,
    selected_lead_hours,
    validate_horizon_policy,
    validate_vertical_column,
)


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
    raw_text = path.read_text(encoding="utf-8")
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


def load_bundle(config_root: Path, product_id: str) -> LoadedBundle:
    """Load and SHA-256 hash a ProductSpec and all four referenced profile YAMLs."""
    p_path = config_root / "products" / f"{product_id}.yaml"
    product = parse_product_spec(parse_declarative_yaml(p_path))
    d_path = config_root / "profiles" / "domains" / f"{product.domain_profile}.yaml"
    s_path = config_root / "profiles" / "science" / f"{product.science_profile}.yaml"
    src1_path = config_root / "profiles" / "sources" / f"{product.primary_source_profile}.yaml"
    src2_path = config_root / "profiles" / "sources" / f"{product.candidate_source_profile}.yaml"
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


def validate_bundle(b: LoadedBundle) -> tuple[int, int, tuple[int, ...]]:
    """Validate horizon SLA, fail-closed candidate source, and vertical BL stretch."""
    remaining_floor = validate_horizon_policy(b.product, b.primary_source, b.output)
    if not b.candidate_source.fail_closed:
        raise ValueError("Candidate ForcingProfile must enforce fail_closed=True")
    bl_counts = [
        validate_vertical_column(
            estimate_auto_levels_agl_m(b.domain, elev_m, etac=b.science.etac), b.domain
        )
        for elev_m in b.domain.terrain_elevations_m
    ]
    return remaining_floor, min(bl_counts), selected_lead_hours(b.output)
