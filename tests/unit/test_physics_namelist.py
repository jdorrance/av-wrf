"""Physics section shapes and scheme-exclusive validation."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.core import adaptive_domains, physics_and_dynamics_sections  # noqa: E402
from av_wrf.core.namelist import validate_science_profile  # noqa: E402
from av_wrf.runner import load_bundle  # noqa: E402


def _product() -> str:
    names = sorted(p.stem for p in (ROOT / "config" / "products").glob("*.yaml"))
    return next(n for n in names if "pilot" in n)


class TestPhysicsNamelist(unittest.TestCase):
    def test_mynn_defaults_emit_lake_and_slope_options(self) -> None:
        sci = load_bundle(ROOT / "config", _product()).science
        sec = physics_and_dynamics_sections(sci, 2)
        self.assertEqual(sec["physics"]["bl_pbl_physics"], (5, 5))
        self.assertEqual(sec["physics"]["icloud"], 1)
        self.assertEqual(sec["physics"]["lakedepth_default"], (50.0, 50.0))
        self.assertEqual(sec["physics"]["use_lakedepth"], (1, 1))
        self.assertEqual(sec["physics"]["lake_min_elev"], (5.0, 5.0))
        self.assertEqual(sec["dynamics"]["diff_6th_slopeopt"], (1, 1))

    def test_adaptive_keys_have_registry_shapes(self) -> None:
        sci = load_bundle(ROOT / "config", _product()).science
        nested = adaptive_domains(sci, 3)
        self.assertEqual(nested["target_cfl"], (1.2, 1.2))
        self.assertEqual(nested["starting_time_step"], (45, 15))
        self.assertIs(nested["use_adaptive_time_step"], True)
        self.assertEqual(adaptive_domains(sci, 2)["starting_time_step"], (30, 15))
        single = adaptive_domains(sci, 1)
        self.assertEqual(single["adaptation_domain"], 1)
        self.assertEqual(single["target_cfl"], 1.2)

    def test_nested_ysu_and_drag_options_reach_each_domain(self) -> None:
        bundle = load_bundle(ROOT / "config", _product())
        science = dataclasses.replace(
            bundle.science,
            bl_pbl_physics=1,
            sf_sfclay_physics=1,
            bl_mynn_edmf=0,
            icloud_bl=0,
            shinhong_scu_mixing=True,
            shinhong_nonlocal_flux=False,
            gwd_opt=1,
            gwd_if_nonhyd=False,
        )
        validate_science_profile(science, bundle.domain)
        for domains in (1, 2):
            sections = physics_and_dynamics_sections(science, domains)
            for group, key, value in (
                ("physics", "shinhong_scu_mixing", True),
                ("physics", "shinhong_nonlocal_flux", False),
                ("dynamics", "gwd_opt", 1),
                ("dynamics", "gwd_if_nonhyd", False),
            ):
                with self.subTest(domains=domains, key=key):
                    expected = (value, value) if domains == 2 else value
                    self.assertEqual(sections[group][key], expected)

    def test_cross_scheme_toggles_are_rejected(self) -> None:
        b = load_bundle(ROOT / "config", _product())
        with self.assertRaisesRegex(ValueError, "topo_wind"):
            validate_science_profile(dataclasses.replace(b.science, topo_wind=1), b.domain)
        with self.assertRaisesRegex(ValueError, "sf_sfclay_physics"):
            validate_science_profile(dataclasses.replace(b.science, sf_sfclay_physics=1), b.domain)
        with self.assertRaisesRegex(ValueError, "coupling requires icloud=1"):
            validate_science_profile(dataclasses.replace(b.science, icloud=3), b.domain)
        with self.assertRaisesRegex(ValueError, "Adaptive steps"):
            validate_science_profile(dataclasses.replace(b.science, max_time_step_s=24), b.domain)
        self.assertEqual(b.science.bl_pbl_physics, 5)


if __name__ == "__main__":
    unittest.main()
