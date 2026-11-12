"""Property-based invariants using Hypothesis when installed (with stdlib sweep fallback)."""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from av_wrf.core import lambert_xy_m_to_latlon, latlon_to_lambert_xy_m  # noqa: E402
from av_wrf.core.thermo import mixing_ratio_to_spfh_vec, rotate_grid_to_earth_vec  # noqa: E402


def _assert_rotation_norm(ug: float, vg: float, angle_rad: float) -> None:
    u, v, cos_a, sin_a = np.array([ug, vg, math.cos(angle_rad), math.sin(angle_rad)]).reshape(4, 1)
    ue, ve = rotate_grid_to_earth_vec(u, v, cos_a, sin_a)
    assert abs(math.hypot(float(ue[0]), float(ve[0])) - math.hypot(ug, vg)) < 1e-9


def _assert_spfh_bounded(r: float) -> None:
    q = float(mixing_ratio_to_spfh_vec(np.array([r]))[0])
    assert 0.0 <= q < 1.0


class TestPropertyInvariants(unittest.TestCase):
    def test_rotation_moisture_and_lambert_invariants(self) -> None:
        try:
            from hypothesis import given
            from hypothesis import strategies as st
        except ImportError:
            for deg in range(-180, 181, 15):
                _assert_rotation_norm(-14.2, 7.8, math.radians(float(deg)))
            for r in (0.0, 1e-5, 0.015, 0.04):
                _assert_spfh_bounded(r)
            return

        @given(
            st.floats(-80.0, 80.0),
            st.floats(-80.0, 80.0),
            st.floats(-math.pi, math.pi),
        )
        def prop_rot(ug: float, vg: float, ang: float) -> None:
            _assert_rotation_norm(ug, vg, ang)

        @given(st.floats(0.0, 0.1))
        def prop_q(r: float) -> None:
            _assert_spfh_bounded(r)

        @given(st.floats(37.0, 43.0), st.floats(41.0, 48.0))
        def prop_lcc(lat: float, lon: float) -> None:
            x, y = latlon_to_lambert_xy_m(
                lat,
                lon,
                origin_lat_deg=40.2,
                central_meridian_deg=44.5,
                std_parallel_1_deg=38.5,
                std_parallel_2_deg=41.8,
                radius_m=6_370_000.0,
            )
            rlat, rlon = lambert_xy_m_to_latlon(
                x,
                y,
                origin_lat_deg=40.2,
                central_meridian_deg=44.5,
                std_parallel_1_deg=38.5,
                std_parallel_2_deg=41.8,
                radius_m=6_370_000.0,
            )
            assert abs(rlat - lat) < 1e-8 and abs(rlon - lon) < 1e-8

        prop_rot()
        prop_q()
        prop_lcc()


if __name__ == "__main__":
    unittest.main()
