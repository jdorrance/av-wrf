"""WPS geogrid/metgrid nest compilation and 30s geography tokens."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.contracts import BuildManifest, RunLock  # noqa: E402
from av_wrf.core import (  # noqa: E402
    compile_geogrid_namelist_sections,
    compile_wps_namelist_sections,
    required_forcing_forecast_hours,
)
from av_wrf.runner import load_bundle  # noqa: E402


def _product(*, pilot: bool = False) -> str:
    names = sorted(p.stem for p in (ROOT / "config" / "products").glob("*.yaml"))
    return next(n for n in names if ("pilot" in n) == pilot)


class TestWpsNamelist(unittest.TestCase):
    def test_nested_geogrid_uses_parent_shape_and_coarse_spacing(self) -> None:
        b = load_bundle(ROOT / "config", _product(pilot=True))
        nested = compile_geogrid_namelist_sections(b.domain, nested=True)
        single = compile_geogrid_namelist_sections(b.domain, nested=False)
        self.assertEqual(nested["share"]["max_dom"], 2)
        self.assertEqual(nested["geogrid"]["e_we"], (101, 103))
        self.assertEqual(nested["geogrid"]["dx"], 9000.0)
        self.assertEqual(single["geogrid"]["e_we"], 103)
        self.assertEqual(single["geogrid"]["geog_data_res"], "modis_lai+30s+default")

    def test_nested_wps_window_and_required_hours(self) -> None:
        b = load_bundle(ROOT / "config", _product(pilot=True))
        lock = RunLock(
            product_id=b.product.product_id,
            model_id=b.product.model_id,
            cycle_partition="20261112/00",
            attempt_id="att-01",
            forcing_cycle_utc="2026-11-11T18:00:00Z",
            wrf_init_utc="2026-11-11T18:00:00Z",
            publish_origin_utc="2026-11-12T00:00:00Z",
            spinup_hours=6,
            lead_hours=(1, 2, 16),
            forcing_profile_id=b.primary_source.profile_id,
            domain_profile_id=b.domain.profile_id,
            science_profile_id=b.science.profile_id,
            output_profile_id=b.output.profile_id,
            config_sha256="a" * 64,
            geog_bundle_sha256="b" * 64,
            build_manifest=BuildManifest("sha256:" + "c" * 64, "w", "p", (), "gf", "mpi"),
            mpi_ranks=4,
            max_dom=2,
            active_domain_id=2,
        )
        wps = compile_wps_namelist_sections(b.domain, b.science, lock, b.primary_source)
        self.assertEqual(wps["share"]["max_dom"], 2)
        self.assertEqual(wps["geogrid"]["i_parent_start"], (1, 34))
        self.assertEqual(required_forcing_forecast_hours(lock, b.primary_source), tuple(range(23)))
        self.assertEqual(dataclasses.replace(lock, lead_hours=(16,)).active_domain_id, 2)


if __name__ == "__main__":
    unittest.main()
