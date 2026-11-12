"""Unit tests for av_wrf.core.vertical: WRF levels(), >20 m AGL usability, and column gates."""

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
    CONSUMER_MIN_LEVEL_AGL_M,
    auto_eta_faces,
    estimate_auto_levels_agl_m,
    validate_frame_columns,
    validate_solver_znw,
    validate_vertical_column,
)
from av_wrf.runner import parse_declarative_yaml  # noqa: E402


class TestVertical(unittest.TestCase):
    def test_auto_eta_faces_match_wrf_480_real_output(self) -> None:
        # ZNW from WRF 4.8.0 real.exe, 55 faces, dzbot=34 m and p_top=5000 Pa.
        golden = (
            1.0,
            0.995790124,
            0.991153479,
            0.986051261,
            0.980441749,
            0.974280775,
            0.967521489,
            0.960114777,
            0.952009678,
            0.943153024,
            0.933490813,
            0.922967911,
            0.911529839,
            0.899122715,
            0.885694683,
            0.871197522,
            0.855587482,
            0.838827491,
            0.820888221,
            0.801750481,
            0.781406581,
            0.759862483,
            0.737139404,
            0.713275015,
            0.688324988,
            0.662363410,
            0.635483146,
            0.607795000,
            0.579427242,
            0.550522923,
            0.521238148,
            0.491738409,
            0.462122619,
            0.432487041,
            0.402934015,
            0.373570919,
            0.344509691,
            0.315865308,
            0.287754595,
            0.260294884,
            0.233602226,
            0.207789555,
            0.182964951,
            0.159229442,
            0.136675254,
            0.115383543,
            0.095422834,
            0.077467546,
            0.061689813,
            0.047825523,
            0.035642598,
            0.024937151,
            0.015530012,
            0.007263739,
            0.0,
        )
        path = next((ROOT / "config" / "profiles" / "domains").glob("*.yaml"))
        domain = dataclasses.replace(
            parse_domain_profile(parse_declarative_yaml(path)),
            e_vert=55,
            dzbot_m=34.0,
            p_top_pa=5000.0,
            dzstretch_s=1.11,
            dzstretch_u=1.06,
            max_dz_m=850.0,
        )
        for actual, expected in zip(auto_eta_faces(domain), golden, strict=True):
            self.assertAlmostEqual(actual, expected, delta=2.0e-6)
        with self.assertRaisesRegex(ValueError, "not enough eta levels"):
            auto_eta_faces(dataclasses.replace(domain, e_vert=51))

    def test_real_upper_fill_can_exceed_max_dz(self) -> None:
        # WRF 4.8.0 real.exe, archived pilot forcing, 51 faces with 1137 m upper fill.
        path = next((ROOT / "config" / "profiles" / "domains").glob("*.yaml"))
        dom = dataclasses.replace(
            parse_domain_profile(parse_declarative_yaml(path)),
            e_vert=51,
            dzbot_m=50.0,
            p_top_pa=5000.0,
            dzstretch_s=1.11,
            dzstretch_u=1.06,
            max_dz_m=850.0,
        )
        eta = auto_eta_faces(dom)
        self.assertEqual(len(eta), 51)
        for idx, expected in ((42, 0.101027526), (49, 0.007551311), (50, 0.0)):
            self.assertAlmostEqual(eta[idx], expected, delta=2e-6)
        with self.assertRaisesRegex(ValueError, "WRF upper eta levels too thick"):
            auto_eta_faces(dataclasses.replace(dom, e_vert=50))

        # PHB-derived AGL midpoints from the same wrfinput column, through the isothermal layer.
        heights = estimate_auto_levels_agl_m(dom, terrain_m=1578.94189453125)
        for idx, expected_m in ((0, 24.72574), (37, 9370.57271), (49, 18482.94011)):
            self.assertAlmostEqual(heights[idx], expected_m, delta=0.01)

    def test_configured_domains_admit_usable_levels_and_reject_unusable_bottom(self) -> None:
        dom_paths = sorted((ROOT / "config" / "profiles" / "domains").glob("*.yaml"))
        self.assertGreaterEqual(len(dom_paths), 1)
        for d_path in dom_paths:
            dom = parse_domain_profile(parse_declarative_yaml(d_path))
            elevs = sorted(dom.terrain_elevations_m)
            cols = [estimate_auto_levels_agl_m(dom, e, etac=0.2, base_temp_k=300.0) for e in elevs]
            for col in cols:
                self.assertGreater(col[0], CONSUMER_MIN_LEVEL_AGL_M)
            counts = [validate_vertical_column(col, dom) for col in cols]
            self.assertGreaterEqual(counts[0], dom.min_bl_levels_2km_agl)
            if len(counts) >= 3:
                self.assertGreater(counts[-1], counts[0])

            # Sub-20 m lowest midpoint (e.g. dzbot=34 m -> ~17 m AGL) is rejected so consumer
            # never discards level 0 via minimumLevelAglM = 20 m.
            low_dzbot = dataclasses.replace(dom, dzbot_m=34.0)
            with self.assertRaises(ValueError):
                validate_vertical_column(
                    estimate_auto_levels_agl_m(low_dzbot, elevs[-1], etac=0.2), low_dzbot
                )

            with self.assertRaises(ValueError):
                estimate_auto_levels_agl_m(dom, terrain_m=5_100.0, etac=0.45)

            unsafe_dz = dataclasses.replace(dom, max_dz_m=980.0)
            with self.assertRaises(ValueError):
                validate_vertical_column(cols[0], unsafe_dz)

            with self.assertRaises(ValueError):
                validate_solver_znw([1.0, 0.5, 0.0], dom)

    def test_local_cfl_diagnostic_and_frame_column_gate_reject_nan_and_gap_bounds(self) -> None:
        dom_path = sorted((ROOT / "config" / "profiles" / "domains").glob("*.yaml"))[0]
        dom = parse_domain_profile(parse_declarative_yaml(dom_path))
        # Standing mountain-wave output Courant proxy > 1.2 is recorded as a diagnostic, not
        # rejected at publish time (RK3 solver CFL is checked on rsl.error.* during solve).
        wave_ok = NormalizedWrfFrame("2026-11-12T01:00:00Z", 0, 3600, {}, {}, 24.0, 820.0, 20, 1.45)
        self.assertEqual(validate_frame_columns(wave_ok, dom), 1.45)
        for bad in (
            dataclasses.replace(wave_ok, min_dz_m=0.0),
            dataclasses.replace(wave_ok, max_dz_m=1000.5),
            dataclasses.replace(wave_ok, min_bl_levels_2km=dom.min_bl_levels_2km_agl - 1),
            dataclasses.replace(wave_ok, max_cfl=-0.01),
            dataclasses.replace(wave_ok, max_cfl=float("nan")),
        ):
            with self.assertRaises(ValueError):
                validate_frame_columns(bad, dom)


if __name__ == "__main__":
    unittest.main()
