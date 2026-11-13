"""Local pipeline integration fixtures."""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

from av_wrf.contracts import DiagnosticsReceipt, ReadyReceipt
from av_wrf.runner.bundle import LoadedBundle, load_bundle
from av_wrf.runner.forcing_recipe import ForcingRecipe
from av_wrf.runner.mirrors import find_forcing_recipe
from av_wrf.runner.store import ObjectStore, json_bytes, published_cycle_prefix, sha256_hex


def ready_with_diagnostics(store: ObjectStore, receipt: ReadyReceipt) -> ReadyReceipt:
    """Bind isolated dispatch fixtures to valid committed diagnostics."""
    raw = json_bytes(
        DiagnosticsReceipt(
            receipt.model_id,
            receipt.cycle_partition,
            receipt.attempt_id,
            (),
            0.0,
            (),
            0.0,
            0.0,
            0.0,
        ).to_doc()
    )
    root = published_cycle_prefix(receipt.model_id, receipt.cycle_partition)
    parent = f"{root}/{receipt.zarr_relpath}".rsplit("/", 1)[0]
    store.put_if_absent(f"{parent}/_diagnostics.json", raw)
    return dataclasses.replace(receipt, diagnostics_sha256=sha256_hex(raw))


def bundle_for_recipe(config: Path, recipe: ForcingRecipe) -> LoadedBundle:
    """Load a product wired to `recipe` as a single-domain bundle for solver stubs."""
    for path in sorted((config / "products").glob("*.yaml")):
        bundle = load_bundle(config, path.stem)
        if find_forcing_recipe(bundle.primary_source) is recipe:
            return dataclasses.replace(
                bundle, domain=dataclasses.replace(bundle.domain, parent_grid_ratio=1)
            )
    raise AssertionError(f"No configured product for {recipe.identity}")


def stage_sparse_config(src_cfg: Path, dst_cfg: Path) -> str:
    """Copy the smallest product configuration with hourly leads 1, 2 and sparse lead 6."""
    shutil.copytree(src_cfg, dst_cfg)
    products = sorted((dst_cfg / "products").glob("*.yaml"))
    candidates = (load_bundle(dst_cfg, path.stem) for path in products)
    bundle = min(candidates, key=lambda item: item.domain.e_we * item.domain.e_sn)
    profile = "wrf-zarr-v3-6h"
    (dst_cfg / "profiles" / "outputs" / f"{profile}.yaml").write_text(
        f"profile_id: {profile}\nschema_id: wrf-zarr-v3\nmap_end_hour: 6\n"
        "hourly_through_hour: 2\ntail_step_hours: 4\nshard_time_steps: 1\n"
        "inner_chunk_yx: [64, 64]\nzstd_level: 3\n",
        encoding="utf-8",
    )
    domain_yaml = dst_cfg / "profiles" / "domains" / f"{bundle.domain.profile_id}.yaml"
    domain_yaml.write_text(
        domain_yaml.read_text(encoding="utf-8")
        .replace("parent_grid_ratio: 3", "parent_grid_ratio: 1")
        .replace("parent_halo_cells: 33", "parent_halo_cells: 0")
        .replace("parent_halo_cells: 30", "parent_halo_cells: 0"),
        encoding="utf-8",
    )
    product = bundle.product
    path = dst_cfg / "products" / f"{product.product_id}.yaml"
    path.write_text(
        path.read_text(encoding="utf-8")
        .replace(f"output_profile: {product.output_profile}", f"output_profile: {profile}")
        .replace(f"min_horizon_hours: {product.min_horizon_hours}", "min_horizon_hours: 1")
        .replace(f"max_cycle_age_hours: {product.max_cycle_age_hours}", "max_cycle_age_hours: 5"),
        encoding="utf-8",
    )
    return product.product_id
