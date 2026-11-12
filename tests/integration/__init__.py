"""Local pipeline integration fixtures."""

from __future__ import annotations

import shutil
from pathlib import Path


def stage_sparse_config(src_cfg: Path, dst_cfg: Path) -> str:
    """Copy the smallest product configuration with hourly leads 1, 2 and sparse lead 6."""
    from av_wrf.runner import load_bundle

    shutil.copytree(src_cfg, dst_cfg)
    candidates = (load_bundle(dst_cfg, path.stem) for path in (dst_cfg / "products").glob("*.yaml"))
    bundle = min(candidates, key=lambda item: item.domain.e_we * item.domain.e_sn)
    profile = "wrf-zarr-v3-6h"
    (dst_cfg / "profiles" / "outputs" / f"{profile}.yaml").write_text(
        f"profile_id: {profile}\nschema_id: wrf-zarr-v3\nmap_end_hour: 6\n"
        "hourly_through_hour: 2\ntail_step_hours: 4\nshard_time_steps: 1\n"
        "inner_chunk_yx: [64, 64]\nzstd_level: 3\n",
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
