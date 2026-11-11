"""Unit tests for av_wrf.core.vertical over valley, Aragats, Ararat, and hybrid folding."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.contracts import parse_domain_profile  # noqa: E402
from av_wrf.core import (  # noqa: E402
    estimate_auto_levels_agl_m,
    validate_vertical_column,
)
from av_wrf.runner import parse_declarative_yaml  # noqa: E402


class TestVertical(unittest.TestCase):
    def test_shipped_armenia_yaml_passes_valley_aragats_and_ararat_gates(self) -> None:
        dom = parse_domain_profile(
            parse_declarative_yaml(
                ROOT / "config" / "profiles" / "domains" / "armenia-3km-v1.yaml"
            )
        )
        col_valley = estimate_auto_levels_agl_m(dom, terrain_m=800.0, etac=0.2)
        col_aragats = estimate_auto_levels_agl_m(dom, terrain_m=4_090.0, etac=0.2)
        col_ararat = estimate_auto_levels_agl_m(dom, terrain_m=5_137.0, etac=0.2)

        bl_valley = validate_vertical_column(col_valley, dom)
        bl_aragats = validate_vertical_column(col_aragats, dom)
        bl_ararat = validate_vertical_column(col_ararat, dom)
        self.assertGreaterEqual(bl_valley, dom.min_bl_levels_2km_agl)
        # Hybrid terrain-following compression increases BL level density over high peaks.
        self.assertGreater(bl_aragats, bl_valley)
        self.assertGreater(bl_ararat, bl_aragats)

        # Excessive etac over 5,137 m Mount Ararat folds mid-tropospheric coordinate surfaces.
        with self.assertRaises(ValueError):
            estimate_auto_levels_agl_m(dom, terrain_m=5_137.0, etac=0.45)

        # max_dz_m > 900 m is rejected to preserve summer thermal-expansion margin (< 1,000 m).
        unsafe_dz = dataclasses.replace(dom, max_dz_m=980.0)
        with self.assertRaises(ValueError):
            validate_vertical_column(col_valley, unsafe_dz)


if __name__ == "__main__":
    unittest.main()
