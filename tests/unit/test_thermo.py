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
from av_wrf.contracts.zarr_schema import WRF_ZARR_V3_FIELDS  # noqa: E402
from av_wrf.core.thermo import (  # noqa: E402
    KAPPA_DRY,
    MOIST_THETA_COEFF,
    WRF_T0_K,
    afwa_mslp_vec,
    clamp_spfh_2m_vec,
    geometric_w_to_omega_vec,
    groom_f32_buf,
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

    def test_native_w_survives_complete_published_moist_round_trip(self) -> None:
        w = _arr(-12.375, -0.0001, 0.0, 0.0001, 12.375)[:, None]
        pressure = _arr(25_123.7, 65_412.3, 99_876.5)
        temp = _arr(220.123, 272.345, 299.789)
        humidity = _arr(0.0, 0.01, 0.025)
        omega = geometric_w_to_omega_vec(w, pressure, temp, humidity)
        keepbits = {field.name: field.keepbits for field in WRF_ZARR_V3_FIELDS}
        fields = dict(pressure_pa=pressure, temp_k=temp, spfh_kgkg=humidity, omega_pas=omega)
        encoded = {
            name: np.asarray(groom_f32_buf(values, keepbits[name]), np.float64).reshape(
                values.shape
            )
            for name, values in fields.items()
        }
        # Independent WRF constants; the proxy is reversible even for strong wave motion.
        recovered = (
            -encoded["omega_pas"]
            * 287.0
            * encoded["temp_k"]
            * (1.0 + (461.6 / 287.0 - 1.0) * encoded["spfh_kgkg"])
            / (encoded["pressure_pa"] * 9.81)
        )
        np.testing.assert_allclose(recovered, np.broadcast_to(w, omega.shape), rtol=4e-5)
        np.testing.assert_array_equal(np.sign(recovered), np.sign(np.broadcast_to(w, omega.shape)))

    def test_consumer_dry_inverse_has_documented_moisture_bias(self) -> None:
        omega = geometric_w_to_omega_vec(_arr(2.0), _arr(85_000.0), _arr(285.0), _arr(0.01))
        # Existing consumer uses dry density and WMO constants; track the cross-repo mismatch.
        recovered = -omega * 287.05 * 285.0 / (85_000.0 * 9.80665)
        np.testing.assert_allclose(recovered / 2.0, _arr(0.9944659240146819), rtol=1e-12)

    def test_cloud_overlap_and_surface_relative_boundary(self) -> None:
        fractions = np.array([[0.25, 0.5], [0.5, 0.25], [0.0, 0.5], [0.5, 0.0]], np.float32)
        pressures = np.broadcast_to(_arr(90_000, 80_000, 70_000, 60_000)[:, None], fractions.shape)
        total, low = max_random_cloud_cover_pct(
            fractions, pressures, np.array([100_000, 120_000], np.float32)
        )
        np.testing.assert_allclose(total, (75.0, 200.0 / 3.0))
        np.testing.assert_array_equal(low, (50.0, 0.0))
        reordered, _ = max_random_cloud_cover_pct(
            fractions[[0, 2, 1, 3]], pressures, np.array([100_000, 120_000], np.float32)
        )
        np.testing.assert_array_equal(reordered, (62.5, 50.0))

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
        mslp = afwa_mslp_vec(
            _arr(100_000, 85_000, 85_000, 70_000, 70_000, 105_000, 105_000),
            _arr(0, 1500, 1500, 3000, 3000, -400, -400),
            _arr(25, 1524, 1524, 3023, 3023, -375, -375),
            _arr(250, 250, 284, 290.5, 305, 280, 305),
            _arr(0.01, 0.005, 0.008, 0.012, 0, 0.01, 0),
        )
        # WRF v4.8.0 module_diag_functions.F MSLP, compiled in default Fortran REAL.
        np.testing.assert_allclose(
            mslp,
            (
                100000,
                103865.3359375,
                101556.703125,
                99487.1875,
                98821.84375,
                100005.8203125,
                100281.8515625,
            ),
            rtol=0.0,
            atol=0.02,
        )

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
