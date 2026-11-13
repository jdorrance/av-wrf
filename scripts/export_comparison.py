"""Export configured comparison products into an av-layers model registry copy."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import cast

from av_wrf.core.grid import computational_latlon_bounds, published_lambert_grid_metadata
from av_wrf.runner import load_bundle


def export_registry(config: Path, campaign: Path, registry: Path) -> dict[str, object]:
    """Preserve the registry and append/replace campaign products from producer-owned geometry."""
    experiment = json.loads(campaign.read_text())
    result: dict[str, object] = json.loads(registry.read_text())
    models = cast(list[dict[str, object]], result["models"])
    consumer = experiment["consumer"]
    template = next(m for m in models if m["modelId"] == consumer["template_model"])
    for product in experiment["products"]:
        bundle = load_bundle(config, product)
        domain, spec, output = bundle.domain, bundle.product, bundle.output
        model = copy.deepcopy(template)
        west, south, east, north = computational_latlon_bounds(
            domain, halo_deg=0, snap_deg=0.000001
        )
        coverage = copy.deepcopy(cast(dict[str, object], template["coverage"]))
        coverage.update(
            id=f"{spec.model_id}-land",
            description=spec.label,
            west=west,
            south=south,
            east=east,
            north=north,
            probes=[[s["id"], s["lat"], s["lon"], True] for s in experiment["sites"]],
        )
        model.update(
            modelId=spec.model_id,
            id=f"{spec.model_id}-soaring",
            label=spec.label,
            attribution=f"{bundle.primary_source.provider} / WRF-ARW / Aviation Forecast Layers",
            coverageDir=f"coverage/{spec.model_id}-land",
            coverage=coverage,
            grid=published_lambert_grid_metadata(domain),
            source={
                "modelFamily": "wrf",
                "configurationId": spec.product_id,
                "domainId": domain.profile_id,
            },
            upstream={"protocol": "wrf-zarr-v3", "provider": "av-wrf", "modelId": spec.model_id},
            terrainSubdivisionFactor=consumer["terrain_subdivision_factor"],
        )
        model["forecast"] = {
            "intervalHours": 1,
            "cycleHoursUtc": [spec.publish_origin_hour_utc],
            "statisticsResetHours": None,
            "expectedRunsPerDay": 1,
            "window": {
                "hourlyThrough": output.hourly_through_hour,
                "endHour": output.map_end_hour,
                "tailStepHours": output.tail_step_hours,
                "minHorizonHours": spec.min_horizon_hours,
                "framesPerShard": 2,
            },
        }
        model["freshness"] = {"maxCycleAgeHours": spec.max_cycle_age_hours}
        cast(dict[str, object], model["capabilities"])["maxVerticalLevels"] = (
            domain.exported_levels_k
        )
        models[:] = [m for m in models if m["modelId"] != spec.model_id]
        models.append(model)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = export_registry(args.config_dir, args.campaign, args.registry)
    with args.out.open("x") as stream:
        stream.write(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
