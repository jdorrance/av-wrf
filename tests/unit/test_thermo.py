"""Reference cases for WRF thermodynamics and surface diagnostics."""

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
from av_wrf.core.thermo import (  # noqa: E402
    KAPPA_DRY,
    MOIST_THETA_COEFF,
    WRF_T0_K,
    clamp_spfh_2m_vec,
    geometric_w_to_omega_vec,
    groom_f32_buf,
    hypsometric_mslp_vec,
    max_random_cloud_cover_pct,
    mixing_ratio_to_spfh_vec,
    parameterized_gust_10m_vec,
    potential_to_temp_vec,
    verify_t_thm_consistency,
)
from av_wrf.runner import load_bundle  # noqa: E402
from tests.oracle.reference_cases import (  # noqa: E402
    EXPECTED_TEMP_850HPA_K,
    PRESSURE_850HPA_PA,
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
        np.testing.assert_allclose(mixing_ratio_to_spfh_vec(_arr(0.01)), (1.0 / 101.0,))
        self.assertLess(abs(groom_f32_buf(_arr(288.13791), 15)[0] - 288.13791), 0.005)
        self.assertLessEqual(abs(groom_f32_buf(_arr(101325.3), 17)[0] - 101325.3), 0.5)

    def test_omega_uses_moist_hydrostatic_density(self) -> None:
        omega = geometric_w_to_omega_vec(_arr(2.0), _arr(85_000.0), _arr(285.0), _arr(0.0, 0.01))
        np.testing.assert_allclose(omega, (-20.38877682009903, -20.265489209784448), rtol=1e-12)

    def test_cloud_overlap_and_surface_relative_boundary(self) -> None:
        fractions = np.array([[0.25, 0.5], [0.5, 0.25], [0.0, 0.5], [0.5, 0.0]], np.float32)
        pressures = np.broadcast_to(_arr(90_000, 80_000, 70_000, 60_000)[:, None], fractions.shape)
        total, low = max_random_cloud_cover_pct(
            fractions, pressures, np.array([100_000, 120_000], np.float32)
        )
        np.testing.assert_allclose(total, (75.0, 200.0 / 3.0))
        np.testing.assert_array_equal(low, (50.0, 0.0))

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

    def test_bolton_q2_clamp(self) -> None:
        t2_k, psfc = 268.15, 80_000.0
        clamped = groom_f32_buf(clamp_spfh_2m_vec(_arr(0.0040), _arr(t2_k), _arr(psfc)), 15)[0]
        r = clamped / (1.0 - clamped)
        e_hpa = (psfc / 100.0) * r / (0.622 + r)
        es_hpa = 6.112 * math.exp((17.67 * -5.0) / (-5.0 + 243.5))
        self.assertLessEqual(e_hpa, es_hpa)
        self.assertGreater(e_hpa, 0.999 * es_hpa)

        t2_k = 333.0
        es_pa = 611.2 * math.exp(17.67 * (t2_k - 273.15) / (t2_k - 29.65))
        psfc = 1.005 * es_pa
        clamped = clamp_spfh_2m_vec(_arr(0.999), _arr(t2_k), _arr(psfc))[0]
        e_pa = psfc * clamped / (0.622 + 0.378 * clamped)
        self.assertLess(e_pa, es_pa)

    def test_mslp_plateau_and_signed_height(self) -> None:
        mslp = hypsometric_mslp_vec(
            _arr(100_000, 85_000, 85_000, 70_000, 70_000, 105_000, 105_000),
            _arr(0, 1500, 1500, 3000, 3000, -400, -400),
            _arr(250, 250, 284, 290.5, 305, 280, 305),
            _arr(0),
        )
        # Scalar hypsometric references with mean temperatures 257.375, 287.25, 290.5,
        # 297.75, 278.7 and 303.7 K for the nonzero heights.
        np.testing.assert_allclose(
            mslp,
            (
                100000,
                103737.291254,
                101610.114508,
                99632.169654,
                98779.495698,
                99973.216679,
                100377.761113,
            ),
            rtol=0.0,
            atol=1e-6,
        )
        p_lo, p_hi = hypsometric_mslp_vec(
            _arr(54_000.0), _arr(5_000.0), _arr(257.99, 258.01), _arr(0)
        )
        self.assertLess(abs(p_hi - p_lo), 5.0)
        p_warm_lo, p_warm_hi = hypsometric_mslp_vec(
            _arr(70_000.0), _arr(3_000.0), _arr(290.49, 290.51), _arr(0)
        )
        self.assertLess(abs(p_warm_hi - p_warm_lo), 5.0)

    def test_gust_matches_ifs_similarity_cases(self) -> None:
        gust = parameterized_gust_10m_vec(
            _arr(3),
            _arr(4),
            _arr(0.3, 0.3, 0.2, 0.2, 0.3),
            _arr(0, -80, 350, 350, 0),
            _arr(0, 20, 80, 80, 180),
            _arr(1000, 1200, 2200, 2200, 1500),
            _arr(290, 290, 290, 290, 298),
            _arr(0.006, 0.006, 0.006, 0.006, 0.018),
            _arr(100_000, 100_000, 100_000, 65_000, 96_000),
        )
        # Scalar IFS eq. 3.98 with L computed before the cube root, using PBLH for z_i.
        np.testing.assert_allclose(
            gust,
            (7.313, 7.313, 10.559086897089, 11.401477883625, 7.559161392627),
            rtol=0.0,
            atol=1e-10,
        )


if __name__ == "__main__":
    unittest.main()
