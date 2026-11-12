"""Concentric nest geometry: trigger, parent shape and footprint containment."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.core import (  # noqa: E402
    computational_latlon_bounds,
    forcing_domain,
    nesting_active,
    parent_domain_shape,
)
from av_wrf.runner import load_bundle  # noqa: E402


def _product(*, pilot: bool = False) -> str:
    names = sorted(p.stem for p in (ROOT / "config" / "products").glob("*.yaml"))
    return next(n for n in names if ("pilot" in n) == pilot)


class TestNesting(unittest.TestCase):
    def test_gfs_triggers_nest_and_parent_covers_child_plus_halos(self) -> None:
        b = load_bundle(ROOT / "config", _product(pilot=True))
        self.assertTrue(nesting_active(b.domain, 0.25))
        self.assertFalse(nesting_active(b.domain, 0.0625))
        parent = forcing_domain(b.domain, 0.25)
        parent_we, parent_sn = parent_domain_shape(b.domain)
        self.assertEqual((parent.e_we, parent.e_sn), (parent_we, parent_sn))
        self.assertEqual(parent.dx_m, b.domain.dx_m * b.domain.parent_grid_ratio)
        self.assertEqual(parent.center_lat_deg, b.domain.center_lat_deg)
        self.assertEqual(parent.center_lon_deg, b.domain.center_lon_deg)

    def test_parent_contains_the_child_footprint(self) -> None:
        b = load_bundle(ROOT / "config", _product())
        parent = forcing_domain(b.domain, 0.25)
        child_w, child_s, child_e, child_n = computational_latlon_bounds(b.domain, halo_deg=0.0)
        par_w, par_s, par_e, par_n = computational_latlon_bounds(parent, halo_deg=0.0)
        self.assertLessEqual(par_w, child_w)
        self.assertLessEqual(par_s, child_s)
        self.assertGreaterEqual(par_e, child_e)
        self.assertGreaterEqual(par_n, child_n)


if __name__ == "__main__":
    unittest.main()
