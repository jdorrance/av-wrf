"""Unit tests for av_wrf.core.thermo: fixed 300K T0, continuous Shuell MSLP, and Bolton Q2."""

from __future__ import annotations

import dataclasses
import math
import sys
import unittest
from pathlib import Path

import numpy as np
import numpy.typing as npt

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.contracts import CompMass3D, Slab3DF32  # noqa: E402
from av_wrf.core import lambert_convergence_trig  # noqa: E402
from av_wrf.core.thermo import (  # noqa: E402
    KAPPA_DRY,
    MOIST_THETA_COEFF,
    WRF_T0_K,
    clamp_spfh_2m_vec,
    geometric_w_to_omega_vec,
    groom_f32_buf,
    hypsometric_mslp_vec,
    max_random_cloud_pct_vec,
    mixing_ratio_to_spfh_vec,
    parameterized_gust_10m_vec,
    potential_to_temp_vec,
    rotate_grid_to_earth_vec,
    verify_t_thm_consistency,
)
from av_wrf.runner import load_bundle  # noqa: E402
from tests.oracle.reference_cases import (  # noqa: E402
    EXPECTED_TEMP_850HPA_K,
    NH_MIDLAT_LCC,
    PRESSURE_850HPA_PA,
    SH_MIDLAT_LCC,
    THETA_PERTURB_10K,
)


def _arr(*values: float) -> npt.NDArray[np.float64]:
    return np.array(values, dtype=np.float64)


class TestThermo(unittest.TestCase):
    def test_sensible_air_temperature_fixed_300k_and_keepbits_bounds(self) -> None:
        self.assertEqual(WRF_T0_K, 300.0)
        self.assertAlmostEqual(KAPPA_DRY, 2.0 / 7.0, places=15)
        t_from_t = potential_to_temp_vec(_arr(THETA_PERTURB_10K), _arr(PRESSURE_850HPA_PA))
        self.assertAlmostEqual(float(t_from_t[0]), EXPECTED_TEMP_850HPA_K, places=11)
        spfh = mixing_ratio_to_spfh_vec(_arr(0.01))
        omega = geometric_w_to_omega_vec(_arr(2.0), _arr(85_000.0), _arr(285.0), spfh)
        self.assertLess(float(omega[0]), -19.0)
        cld = max_random_cloud_pct_vec(_arr(0.0, 0.5, 0.5)[:, None])
        self.assertAlmostEqual(float(cld[0]), 50.0, places=5)
        self.assertLess(abs(groom_f32_buf(_arr(288.13791), 15)[0] - 288.13791), 0.005)
        self.assertLessEqual(abs(groom_f32_buf(_arr(101325.3), 17)[0] - 101325.3), 0.5)

    def test_groom_rounds_ties_to_even_and_preserves_special_values(self) -> None:
        step = 2.0**-15
        for sign in (1.0, -1.0):
            ties = sign * _arr(1.0 + step / 2, 1.0 + 1.5 * step)
            self.assertEqual(list(groom_f32_buf(ties, 15)), [sign, sign * (1 + 2 * step)])
        zero, inf, nan = groom_f32_buf(_arr(-0.0, float("inf"), float("nan")), 15)
        self.assertEqual(math.copysign(1.0, zero), -1.0)
        self.assertEqual(inf, float("inf"))
        self.assertTrue(math.isnan(nan))

    def test_t_thm_consistency_with_non300_base_temp(self) -> None:
        prod_id = sorted((ROOT / "config" / "products").glob("*.yaml"))[0].stem
        sci_290 = dataclasses.replace(
            load_bundle(ROOT / "config", prod_id).science, base_temp_k=290.0
        )
        n = 1000
        t_dry = CompMass3D(Slab3DF32.from_values(10, 10, 10, [8.0] * n))
        qv = CompMass3D(Slab3DF32.from_values(10, 10, 10, [0.012] * n))
        p0 = CompMass3D(Slab3DF32.from_values(10, 10, 10, [0.0] * n))
        pb = CompMass3D(Slab3DF32.from_values(10, 10, 10, [85_000.0] * n))
        # WRF perturbation offset is ALWAYS 300 K even when namelist base_temp is 290 K
        thm_v = (8.0 + 300.0) * (1.0 + MOIST_THETA_COEFF * 0.012) - 300.0
        thm_vals = [thm_v] * n
        verify_t_thm_consistency(
            t_dry, CompMass3D(Slab3DF32.from_values(10, 10, 10, thm_vals)), p0, pb, qv, sci_290
        )
        thm_vals[777] = 8.0
        with self.assertRaises(ValueError):
            verify_t_thm_consistency(
                t_dry, CompMass3D(Slab3DF32.from_values(10, 10, 10, thm_vals)), p0, pb, qv, sci_290
            )

    def test_bolton_q2_clamp_continuous_shuell_mslp_and_high_altitude_gusts(self) -> None:
        t2_k, psfc = 268.15, 80_000.0
        clamped = groom_f32_buf(clamp_spfh_2m_vec(_arr(0.0040), _arr(t2_k), _arr(psfc)), 15)[0]
        r = clamped / (1.0 - clamped)
        e_hpa = (psfc / 100.0) * r / (0.622 + r)
        es_hpa = 6.112 * math.exp((17.67 * -5.0) / (-5.0 + 243.5))
        self.assertLessEqual(e_hpa, es_hpa)
        self.assertGreater(e_hpa, 0.999 * es_hpa)

        # Zero discontinuity across the 258 K / 5,000 m (tv_sl = 290.5 K) boundary
        p_lo, p_hi = hypsometric_mslp_vec(
            _arr(54_000.0), _arr(5_000.0), _arr(257.99, 258.01), _arr(0)
        )
        self.assertLess(abs(p_hi - p_lo), 5.0)  # < 5 Pa (0.05 hPa), never 600 Pa (6 hPa)!
        # Also continuous across tv_0 = 290.5 K
        p_warm_lo, p_warm_hi = hypsometric_mslp_vec(
            _arr(70_000.0), _arr(3_000.0), _arr(290.49, 290.51), _arr(0)
        )
        self.assertLess(abs(p_warm_hi - p_warm_lo), 5.0)

        unclamped_tv = 305.0 + 0.5 * 0.0065 * 3000.0
        naive_mslp = 70_000.0 * math.exp((9.81 * 3000.0) / (287.0 * unclamped_tv))
        shuell_mslp = hypsometric_mslp_vec(_arr(70_000.0), _arr(3000.0), _arr(305.0), _arr(0.008))[
            0
        ]
        self.assertGreater(shuell_mslp - naive_mslp, 300.0)

        g_sea, g_mtn = parameterized_gust_10m_vec(
            _arr(3),
            _arr(0),
            _arr(0.2),
            _arr(350),
            _arr(80),
            _arr(2200),
            _arr(290),
            _arr(0.006),
            _arr(100_000, 65_000),
        )
        self.assertGreater(g_mtn, g_sea + 0.5)

    def test_wrf_alpha_matches_consumer_in_nh_and_sh(self) -> None:
        for lat0, lon0, sp1, sp2 in (NH_MIDLAT_LCC, SH_MIDLAT_LCC):
            cos_t, sin_t = lambert_convergence_trig(
                lon0 + 4.0,
                central_meridian_deg=lon0,
                std_parallel_1_deg=sp1,
                std_parallel_2_deg=sp2,
                origin_lat_deg=lat0,
                radius_m=6_370_000.0,
            )
            ue, ve = rotate_grid_to_earth_vec(_arr(7.3), _arr(-4.1), _arr(cos_t), _arr(-sin_t))
            self.assertAlmostEqual(float(ue[0]), cos_t * 7.3 + sin_t * (-4.1), places=12)
            self.assertAlmostEqual(float(ve[0]), -sin_t * 7.3 + cos_t * (-4.1), places=12)


if __name__ == "__main__":
    unittest.main()
