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
from av_wrf.core import RawWrfStepInput, build_accum_baseline, post_frame  # noqa: E402
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

    def test_selected_cloud_cover_preserves_clear_and_overcast_columns(self) -> None:
        for fraction in (0.25, 0.0, 1.0):
            with self.subTest(fraction=fraction):
                np.frombuffer(self.raw.cldfra.buf, np.float32).fill(fraction)
                _, fields, _ = post_3d_slabs(
                    self.raw, self.domain, self.bundle.science, self.shared
                )
                for name in ("cloud_total_pct", "cloud_low_pct"):
                    np.testing.assert_array_equal(
                        np.frombuffer(fields[name].buf, np.float32), fraction * 100.0
                    )

    def test_pressure_validation_includes_levels_above_exported_column(self) -> None:
        pb = np.frombuffer(self.raw.pb_pa.buf, np.float32).reshape(
            self.raw.pb_pa.nz, self.raw.pb_pa.ny, self.raw.pb_pa.nx
        )
        for value, message in ((0.0, "Non-positive"), (88_000.0, "Non-monotonic")):
            with self.subTest(value=value):
                pb[-1].fill(value)
                with self.assertRaisesRegex(ValueError, message):
                    post_3d_slabs(self.raw, self.domain, self.bundle.science, self.shared)

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

    def test_post_rejects_invalid_faces_even_with_increasing_midpoints_and_zero_w(self) -> None:
        raw, domain = self.raw, self.domain
        shape = (raw.phb_stag_m2s2.nz, raw.phb_stag_m2s2.ny, raw.phb_stag_m2s2.nx)
        phb = np.frombuffer(raw.phb_stag_m2s2.buf, np.float32).reshape(shape)
        terrain = np.frombuffer(raw.terrain_m.buf, np.float32).reshape(shape[1:])
        np.frombuffer(raw.ph_stag_m2s2.buf, np.float32).fill(0.0)
        np.frombuffer(raw.w_stag_mps.buf, np.float32).fill(0.0)
        baseline = build_accum_baseline(
            dataclasses.replace(raw, valid_time_unix_s=raw.valid_time_unix_s - 3600),
            domain.bdy_trim_cells,
        )
        for face2_m in (120.0, 50.0, 60.0, float("nan"), float("inf")):
            with self.subTest(face2_m=face2_m):
                faces = np.asarray((0.0, 60.0, face2_m, 160.0))
                phb[:4] = (terrain[None, :, :] + faces[:, None, None]) * 9.81
                if face2_m == 120.0:
                    post_frame(baseline, raw, domain, self.bundle.science)
                else:
                    # Both folded cases have valid increasing midpoints above 20 m AGL.
                    if np.isfinite(face2_m):
                        self.assertTrue(np.all(np.diff((faces[1:] + faces[:-1]) / 2) > 0))
                    with self.assertRaisesRegex(ValueError, "geopotential"):
                        post_frame(baseline, raw, domain, self.bundle.science)


if __name__ == "__main__":
    unittest.main()
