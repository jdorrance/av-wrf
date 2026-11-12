"""Unit tests for av_wrf.core.metrics: interpolated [zi, zi+band_m] cap and BL shear."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from av_wrf.core import (  # noqa: E402
    capping_inversion_strength_k,
    interpolate_at_height_agl,
    mixed_layer_depth_agl_m,
    pressure_tendency_abs_hpa_hr,
    vector_wind_shear_mps,
)
from av_wrf.verify import SoundingObservation, evaluate_column_soaring_proxies  # noqa: E402


class TestMetrics(unittest.TestCase):
    def test_interpolated_inversion_band_and_bl_shear_excludes_stratosphere(self) -> None:
        z = (25.0, 500.0, 1000.0, 1500.0, 2100.0, 2600.0, 12000.0)
        tv = (300.0, 300.1, 300.2, 300.3, 303.3, 305.0, 340.0)
        u = (2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 45.0)
        v = (1.0, 1.0, 1.0, 5.0, 5.0, 5.0, 30.0)
        zi = mixed_layer_depth_agl_m(z, tv, excess_k=0.5)
        self.assertTrue(1500.0 < zi < 1600.0)
        self.assertAlmostEqual(interpolate_at_height_agl(z, u, 1250.0), 4.5, places=9)

        # Zero discrete nodes lie inside (1550, 1850), yet linear endpoint interpolation recovers
        # exact +1.5 K across 300 m (slope = 3.0 K / 600 m = 0.005 K/m)!
        cap_coarse = capping_inversion_strength_k(z, tv, zi_agl_m=1550.0, band_m=300.0)
        self.assertAlmostEqual(cap_coarse, 1.5, places=6)

        # Truncated column ending below zi + band_m fails closed rather than returning 0.0
        with self.assertRaises(ValueError):
            capping_inversion_strength_k((25.0, 1000.0, 1600.0), (300.0, 300.2, 301.0), 1500.0)

        self.assertAlmostEqual(vector_wind_shear_mps(2.0, 1.0, 5.0, 5.0), 5.0, places=9)
        obs = SoundingObservation(
            station_id="UGEE",
            valid_time_utc="2026-07-15T12:00:00Z",
            heights_agl_m=z,
            theta_v_k=tv,
            u_mps=u,
            v_mps=v,
        )
        summary = evaluate_column_soaring_proxies(obs)
        self.assertGreater(summary.capping_delta_theta_v_k, 1.4)
        self.assertTrue(5.0 < summary.bulk_shear_mps < 6.0)
        self.assertAlmostEqual(
            pressure_tendency_abs_hpa_hr((90000.0, 91000.0), (90150.0, 90850.0), 1.0),
            1.5,
            places=6,
        )


if __name__ == "__main__":
    unittest.main()
