"""Published cloud cover and the true-thickness Courant proxy."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.codecs import build_synthetic_wrf_step  # noqa: E402
from av_wrf.core import RawWrfStepInput  # noqa: E402
from av_wrf.core.post_3d import crop2_f64, post_3d_slabs  # noqa: E402
from av_wrf.runner import load_bundle  # noqa: E402


class TestPost3d(unittest.TestCase):
    def setUp(self) -> None:
        products = sorted(p.stem for p in (ROOT / "config" / "products").glob("*.yaml"))
        self.bundle = load_bundle(ROOT / "config", next(p for p in products if "pilot" in p))
        self.domain = dataclasses.replace(
            self.bundle.domain,
            e_we=6,
            e_sn=5,
            e_vert=55,
            exported_levels_k=3,
            bdy_trim_cells=1,
            min_bl_levels_2km_agl=1,
        )
        self.raw: RawWrfStepInput = build_synthetic_wrf_step(
            self.domain, "2026-11-12T00:00:00Z", 1_000_000, 0
        )
        trim = self.domain.bdy_trim_cells
        self.shared = (
            crop2_f64(self.raw.terrain_m, trim, "HGT"),
            crop2_f64(self.raw.psfc_pa, trim, "PSFC"),
            crop2_f64(self.raw.cos_alpha, trim, "COSALPHA"),
            crop2_f64(self.raw.sin_alpha, trim, "SINALPHA"),
        )

    def test_mynn_cloud_cover_uses_current_fraction_and_archive_fallback(self) -> None:
        np.frombuffer(self.raw.cldfra.buf, np.float32).fill(0.75)
        assert self.raw.cldfra_bl is not None
        # A dissipating or disappearing cloud must replace its older radiation snapshot.
        for fraction in (0.25, 0.0, 1.0):
            with self.subTest(fraction=fraction):
                np.frombuffer(self.raw.cldfra_bl.buf, np.float32).fill(fraction)
                _, fields, _ = post_3d_slabs(
                    self.raw, self.domain, self.bundle.science, self.shared
                )
                for name in ("cloud_total_pct", "cloud_low_pct"):
                    np.testing.assert_array_equal(
                        np.frombuffer(fields[name].buf, np.float32), fraction * 100.0
                    )
        _, fallback, _ = post_3d_slabs(
            dataclasses.replace(self.raw, cldfra_bl=None),
            self.domain,
            self.bundle.science,
            self.shared,
        )
        for name in ("cloud_total_pct", "cloud_low_pct"):
            np.testing.assert_array_equal(np.frombuffer(fallback[name].buf, np.float32), 75.0)

    def test_inactive_mynn_field_preserves_radiation_cloud(self) -> None:
        np.frombuffer(self.raw.cldfra.buf, np.float32).fill(0.75)
        assert self.raw.cldfra_bl is not None
        np.frombuffer(self.raw.cldfra_bl.buf, np.float32).fill(0.25)
        for pbl_scheme, coupling in ((1, 1), (5, 0)):
            with self.subTest(pbl_scheme=pbl_scheme, coupling=coupling):
                science = dataclasses.replace(
                    self.bundle.science, bl_pbl_physics=pbl_scheme, icloud_bl=coupling
                )
                _, fields, _ = post_3d_slabs(self.raw, self.domain, science, self.shared)
                for name in ("cloud_total_pct", "cloud_low_pct"):
                    np.testing.assert_array_equal(np.frombuffer(fields[name].buf, np.float32), 75.0)

    def test_sampled_timestep_and_column_diagnostics(self) -> None:
        _, f2d, (min_dz, max_dz, bl, cfl) = post_3d_slabs(
            self.raw, self.domain, self.bundle.science, self.shared
        )
        _, _, (_, _, _, half_cfl) = post_3d_slabs(
            dataclasses.replace(self.raw, dt_s=7.5),
            self.domain,
            self.bundle.science,
            self.shared,
        )
        self.assertAlmostEqual(half_cfl * 2, cfl, places=4)
        self.assertIn("mslp_pa", f2d)
        self.assertGreater(min_dz, 0.0)
        self.assertGreaterEqual(max_dz, min_dz)
        self.assertGreaterEqual(bl, 1)
        self.assertGreater(cfl, 0.0)


if __name__ == "__main__":
    unittest.main()
