"""Unit tests for av_wrf.core.forcing fail-closed check and Noah-MP soil interpolation."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from av_wrf.core import interpolate_soil_profile, verify_forcing_completeness  # noqa: E402


class TestForcing(unittest.TestCase):
    def test_fail_closed_forcing_completeness(self) -> None:
        req = frozenset({"TMP:850", "TMP:700", "HGT:500"})
        verify_forcing_completeness(req, req)
        with self.assertRaises(ValueError):
            verify_forcing_completeness(req, frozenset({"TMP:850", "TMP:700"}))
        with self.assertRaises(ValueError):
            verify_forcing_completeness(req, req, nonfinite_keys=frozenset({"TMP:700"}))

    def test_ifs_to_noah_mp_soil_interpolation_and_bounds(self) -> None:
        ifs_depths = (3.5, 17.5, 64.0, 194.5)
        ifs_smois = (0.18, 0.24, 0.30, 0.55)
        out = interpolate_soil_profile(
            ifs_depths, ifs_smois, lower_bound=0.05, upper_bound=0.45
        )
        self.assertEqual(len(out), 4)
        self.assertGreater(out[0], 0.18)
        self.assertEqual(out[3], 0.45)  # clamped to porosity ceiling


if __name__ == "__main__":
    unittest.main()
