"""Configured resolution experiment: matched parent, clocks, coverage and consumer export."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from scripts.export_comparison import export_registry

from av_wrf.core.grid import (
    computational_latlon_bounds,
    latlon_to_lambert_xy_m,
    published_lambert_grid_metadata,
)
from av_wrf.core.namelist import compile_wrf_namelist_sections
from av_wrf.core.nesting import forcing_domain, nesting_active
from av_wrf.runner import load_bundle

ROOT = Path(__file__).resolve().parents[2]
CAMPAIGN = next((ROOT / "config/comparisons").glob("*.json"))


def test_comparison_parent_clocks_and_support() -> None:
    experiment = json.loads(CAMPAIGN.read_text())
    bundles = [load_bundle(ROOT / "config", product) for product in experiment["products"]]
    control, fine = sorted(bundles, key=lambda b: b.domain.dx_m, reverse=True)
    assert not nesting_active(control.domain, control.primary_source.grid_spacing_deg)
    assert nesting_active(fine.domain, fine.primary_source.grid_spacing_deg)
    parent = forcing_domain(fine.domain, fine.primary_source.grid_spacing_deg)
    assert published_lambert_grid_metadata(parent) == published_lambert_grid_metadata(
        control.domain
    )
    ignored = {"profile_id", "time_step_s", "min_time_step_s", "max_time_step_s"}
    assert {k: v for k, v in dataclasses.asdict(control.science).items() if k not in ignored} == {
        k: v for k, v in dataclasses.asdict(fine.science).items() if k not in ignored
    }
    for bundle in bundles:
        assert bundle.primary_source.provider == "DWD"
        assert bundle.output.map_end_hour == bundle.output.hourly_through_hour == 24
        assert bundle.product.spinup_hours == 6
        assert not bundle.science.use_adaptive_time_step
        domain = bundle.domain
        grid = published_lambert_grid_metadata(domain)
        for site in experiment["sites"]:
            x, y = latlon_to_lambert_xy_m(
                site["lat"],
                site["lon"],
                origin_lat_deg=domain.center_lat_deg,
                central_meridian_deg=domain.central_meridian_deg,
                std_parallel_1_deg=domain.std_parallel_1_deg,
                std_parallel_2_deg=domain.std_parallel_2_deg,
                radius_m=domain.earth_radius_m,
            )
            # All comparison probes have at least one interpolation cell of published support.
            assert float(grid["x0M"]) + domain.dx_m < x < -float(grid["x0M"]) - domain.dx_m
            assert float(grid["y0M"]) + domain.dy_m < y < -float(grid["y0M"]) - domain.dy_m
    sections = compile_wrf_namelist_sections(
        fine.domain, fine.science, fine.primary_source, nested=True
    )
    assert sections["domains"]["time_step"] == control.science.time_step_s == 15
    assert sections["domains"]["parent_time_step_ratio"] == (1, 3)
    assert sections["domains"]["feedback"] == 0
    assert sections["physics"]["bl_pbl_physics"] == (5, 5)
    assert fine.science.time_step_s == 5


def test_consumer_export_is_repeatable_and_preserves_other_models(tmp_path: Path) -> None:
    campaign = json.loads(CAMPAIGN.read_text())
    template = {
        "modelId": campaign["consumer"]["template_model"],
        "coverage": {},
        "capabilities": {},
        "execution": {"schedule": ["active-template-schedule"]},
    }
    registry = tmp_path / "registry.json"
    original = {"models": [template, {"modelId": "unchanged"}]}
    registry.write_text(json.dumps(original))
    exported = export_registry(ROOT / "config", CAMPAIGN, registry)
    models = exported["models"]
    assert isinstance(models, list)
    assert models[:2] == original["models"]
    registry.write_text(json.dumps(exported))
    assert export_registry(ROOT / "config", CAMPAIGN, registry) == exported
    for model in models[2:]:
        bundle = load_bundle(ROOT / "config", model["modelId"])
        assert model["upstream"]["modelId"] == bundle.product.model_id
        assert model["grid"] == published_lambert_grid_metadata(bundle.domain)
        assert model["forecast"]["window"]["endHour"] == 24
        assert model["terrainSubdivisionFactor"] == 1
        assert model["execution"]["schedule"] == []
        coverage = model["coverage"]
        west, south, east, north = computational_latlon_bounds(
            bundle.domain, halo_deg=0, snap_deg=0.0001
        )
        assert west < coverage["west"] < coverage["east"] < east
        assert south < coverage["south"] < coverage["north"] < north
