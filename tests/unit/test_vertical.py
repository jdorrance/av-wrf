"""Unit tests for av_wrf.core.vertical: real.exe e_vert=51 rejection, ZNW check, and NaN guards."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.contracts import NormalizedWrfFrame, parse_domain_profile  # noqa: E402
from av_wrf.core import (  # noqa: E402
    estimate_auto_levels_agl_m,
    local_cfl_3d,
    validate_frame_columns,
    validate_solver_znw,
    validate_vertical_column,
)
from av_wrf.runner import parse_declarative_yaml  # noqa: E402


class TestVertical(unittest.TestCase):
    def test_configured_domains_pass_55_faces_reject_51_faces_and_reject_folding(self) -> None:
        dom_paths = sorted((ROOT / "config" / "profiles" / "domains").glob("*.yaml"))
        self.assertGreaterEqual(len(dom_paths), 1)
        for d_path in dom_paths:
            dom = parse_domain_profile(parse_declarative_yaml(d_path))
            elevs = sorted(dom.terrain_elevations_m)
            counts = [
                validate_vertical_column(estimate_auto_levels_agl_m(dom, e, etac=0.2), dom)
                for e in elevs
            ]
            self.assertGreaterEqual(counts[0], dom.min_bl_levels_2km_agl)
            if len(counts) >= 3:
                self.assertGreater(counts[-1], counts[0])

            # 51 faces cannot reach 5,000 Pa under real.exe auto_levels_opt=2 log-p stretch
            with self.assertRaises(ValueError):
                estimate_auto_levels_agl_m(dataclasses.replace(dom, e_vert=51), elevs[0], etac=0.2)

            with self.assertRaises(ValueError):
                estimate_auto_levels_agl_m(dom, terrain_m=5_100.0, etac=0.45)

            unsafe_dz = dataclasses.replace(dom, max_dz_m=980.0)
            col0 = estimate_auto_levels_agl_m(dom, elevs[0], etac=0.2)
            with self.assertRaises(ValueError):
                validate_vertical_column(col0, unsafe_dz)

            # Solver ZNW validation rejects non-monotonic or wrong-length ZNW
            with self.assertRaises(ValueError):
                validate_solver_znw([1.0, 0.5, 0.0], dom)

    def test_local_cfl_and_frame_column_gate_reject_nan_and_bounds(self) -> None:
        dom_path = sorted((ROOT / "config" / "profiles" / "domains").glob("*.yaml"))[0]
        dom = parse_domain_profile(parse_declarative_yaml(dom_path))
        cfl = local_cfl_3d(10.0, 5.0, 2.0, 40.0, 3000.0, 3000.0, 15.0)
        self.assertAlmostEqual(cfl, (10.0 / 3000.0 + 5.0 / 3000.0 + 2.0 / 40.0) * 15.0)
        with self.assertRaises(ValueError):
            local_cfl_3d(float("nan"), 5.0, 2.0, 40.0, 3000.0, 3000.0, 15.0)

        ok = NormalizedWrfFrame("2026-11-12T01:00:00Z", 0, 3600, {}, {}, 16.0, 820.0, 20, 0.42)
        self.assertEqual(validate_frame_columns(ok, dom), 0.42)
        for bad in (
            dataclasses.replace(ok, min_dz_m=0.0),
            dataclasses.replace(ok, max_dz_m=1000.5),
            dataclasses.replace(ok, min_bl_levels_2km=dom.min_bl_levels_2km_agl - 1),
            dataclasses.replace(ok, max_cfl=1.35),
            dataclasses.replace(ok, max_cfl=float("nan")),
        ):
            with self.assertRaises(ValueError):
                validate_frame_columns(bad, dom)


if __name__ == "__main__":
    unittest.main()
