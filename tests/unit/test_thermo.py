"""Unit tests for av_wrf.core.thermo: fixed 300K T0, continuous Shuell MSLP, and Bolton Q2."""

from __future__ import annotations

import dataclasses
import math
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.contracts import (  # noqa: E402
    CompMass3D,
    GridWindMps,
    MixingRatioKgKg,
    Pascals,
    Slab3DF32,
    SpecificHumidityKgKg,
    TempK,
    VerticalVelocityMps,
    WrfPerturbMoistThetaK,
    WrfPerturbPotTempK,
)
from av_wrf.core import (  # noqa: E402
    KAPPA_DRY,
    MOIST_THETA_COEFF,
    WRF_T0_K,
    clamp_spfh_2m_to_liquid_sat,
    geometric_w_to_omega_pas,
    groom_f32_buf,
    groom_mantissa_f32,
    hypsometric_mslp_pa,
    lambert_convergence_trig,
    max_random_cloud_pct,
    mixing_ratio_to_spfh,
    moist_thm_to_temp_k,
    parameterized_gust_10m_mps,
    potential_to_temp_k,
    rotate_grid_to_earth,
    verify_t_thm_consistency,
    verify_wrf_alpha_against_lambert,
)
from av_wrf.runner import load_bundle  # noqa: E402
from tests.oracle.reference_cases import (  # noqa: E402
    EXPECTED_TEMP_850HPA_K,
    NH_MIDLAT_LCC,
    PRESSURE_850HPA_PA,
    SH_MIDLAT_LCC,
    THETA_PERTURB_10K,
)


class TestThermo(unittest.TestCase):
    def test_sensible_air_temperature_fixed_300k_and_keepbits_bounds(self) -> None:
        self.assertEqual(WRF_T0_K, 300.0)
        self.assertAlmostEqual(KAPPA_DRY, 2.0 / 7.0, places=15)
        t_from_t = potential_to_temp_k(
            WrfPerturbPotTempK(THETA_PERTURB_10K), Pascals(PRESSURE_850HPA_PA)
        )
        self.assertAlmostEqual(float(t_from_t), EXPECTED_TEMP_850HPA_K, places=11)

        qv = 0.014
        thm_pert = (THETA_PERTURB_10K + WRF_T0_K) * (1.0 + MOIST_THETA_COEFF * qv) - WRF_T0_K
        t_from_thm = moist_thm_to_temp_k(
            WrfPerturbMoistThetaK(thm_pert),
            MixingRatioKgKg(qv),
            Pascals(PRESSURE_850HPA_PA),
        )
        self.assertAlmostEqual(float(t_from_t), float(t_from_thm), places=11)

        spfh = mixing_ratio_to_spfh(MixingRatioKgKg(0.01))
        omega = geometric_w_to_omega_pas(
            VerticalVelocityMps(2.0),
            Pascals(85_000.0),
            TempK(285.0),
            SpecificHumidityKgKg(float(spfh)),
        )
        self.assertLess(float(omega), -19.0)
        self.assertAlmostEqual(max_random_cloud_pct([0.0, 0.5, 0.5]), 50.0, places=5)
        self.assertLess(abs(groom_mantissa_f32(288.13791, keepbits=15) - 288.13791), 0.005)
        self.assertLessEqual(abs(groom_mantissa_f32(101325.3, keepbits=17) - 101325.3), 0.5)

    def test_vectorized_groom_and_fixed_t0_with_non300_base_temp(self) -> None:
        samples = [0.0, -0.0, 288.13791, -33.333336, 101325.3, -12.8751, 1e-6, float("inf")]
        arr = np.array(samples, dtype=np.float64)
        for kb in (15, 17, 23):
            vec = list(groom_f32_buf(arr, kb))
            ref = [groom_mantissa_f32(float(np.float32(v)), kb) for v in samples]
            self.assertEqual(vec, ref)

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
        clamped = groom_mantissa_f32(clamp_spfh_2m_to_liquid_sat(0.0040, t2_k, psfc), 15)
        r = clamped / (1.0 - clamped)
        e_hpa = (psfc / 100.0) * r / (0.622 + r)
        es_hpa = 6.112 * math.exp((17.67 * -5.0) / (-5.0 + 243.5))
        self.assertLessEqual(e_hpa, es_hpa)
        self.assertGreater(e_hpa, 0.999 * es_hpa)

        # Zero discontinuity across the 258 K / 5,000 m (tv_sl = 290.5 K) boundary
        p_lo = hypsometric_mslp_pa(54_000.0, 5_000.0, 257.99, 0.0)
        p_hi = hypsometric_mslp_pa(54_000.0, 5_000.0, 258.01, 0.0)
        self.assertLess(abs(p_hi - p_lo), 5.0)  # < 5 Pa (0.05 hPa), never 600 Pa (6 hPa)!
        # Also continuous across tv_0 = 290.5 K
        p_warm_lo = hypsometric_mslp_pa(70_000.0, 3_000.0, 290.49, 0.0)
        p_warm_hi = hypsometric_mslp_pa(70_000.0, 3_000.0, 290.51, 0.0)
        self.assertLess(abs(p_warm_hi - p_warm_lo), 5.0)

        unclamped_tv = 305.0 + 0.5 * 0.0065 * 3000.0
        naive_mslp = 70_000.0 * math.exp((9.81 * 3000.0) / (287.0 * unclamped_tv))
        shuell_mslp = hypsometric_mslp_pa(70_000.0, 3000.0, 305.0, 0.008)
        self.assertGreater(shuell_mslp - naive_mslp, 300.0)

        g_sea = parameterized_gust_10m_mps(
            3.0, 0.0, 3.0, 0.2, 350.0, 80.0, 2200.0, 290.0, 0.006, 100_000.0
        )
        g_mtn = parameterized_gust_10m_mps(
            3.0, 0.0, 3.0, 0.2, 350.0, 80.0, 2200.0, 290.0, 0.006, 65_000.0
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
            verify_wrf_alpha_against_lambert(cos_t, -sin_t, cos_t, sin_t)
            ue, ve = rotate_grid_to_earth(GridWindMps(7.3), GridWindMps(-4.1), cos_t, -sin_t)
            self.assertAlmostEqual(float(ue), cos_t * 7.3 + sin_t * (-4.1), places=12)
            self.assertAlmostEqual(float(ve), -sin_t * 7.3 + cos_t * (-4.1), places=12)


if __name__ == "__main__":
    unittest.main()
