"""Unit tests for av_wrf.core.post: signed fluxes, pressure ordering, fixed 300K, and NaN guards."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.contracts import (  # noqa: E402
    WRF_ZARR_V3_FIELD_NAMES,
    CompBucket2D,
    CompMass2D,
    CompMass3D,
    CompStagX3D,
    CompStagY3D,
    CompStagZ3D,
    Slab2DF32,
    Slab2DI32,
    Slab3DF32,
)
from av_wrf.core import (  # noqa: E402
    KAPPA_DRY,
    P0_PA,
    RawWrfStepInput,
    build_accum_baseline,
    post_frame,
    validate_frame_columns,
)
from av_wrf.runner import load_bundle  # noqa: E402


def _synthetic_step(
    unix_s: int,
    iso_utc: str,
    ny: int,
    nx: int,
    nz: int,
    *,
    achfx: float,
    rainnc: float,
    i_rainnc: int,
    dz_face_m: float = 150.0,
) -> RawWrfStepInput:
    n2 = ny * nx

    def c2(val: float) -> CompMass2D:
        return CompMass2D(Slab2DF32.from_values(ny, nx, [val] * n2))

    def ci(val: int) -> CompBucket2D:
        return CompBucket2D(Slab2DI32.from_values(ny, nx, [val] * n2))

    def c3(val: float) -> CompMass3D:
        return CompMass3D(Slab3DF32.from_values(nz, ny, nx, [val] * (nz * n2)))

    pb_vals = [85_000.0 * (0.95**k) for k in range(nz) for _ in range(n2)]
    ph_vals = [0.0] * ((nz + 1) * n2)
    phb_vals = [(1200.0 + dz_face_m * k) * 9.81 for k in range(nz + 1) for _ in range(n2)]
    w_vals = [0.2 * (k + 1) for k in range(nz + 1) for _ in range(n2)]
    u_buf = [8.0] * (nz * ny * (nx + 1))
    v_buf = [2.0] * (nz * (ny + 1) * nx)
    return RawWrfStepInput(
        valid_time_utc=iso_utc,
        valid_time_unix_s=unix_s,
        terrain_m=c2(1200.0),
        cos_alpha=c2(1.0),
        sin_alpha=c2(0.0),
        psfc_pa=c2(88_000.0),
        t2_k=c2(292.0),
        q2_kgkg=c2(0.008),
        u10_grid_mps=c2(4.0),
        v10_grid_mps=c2(-3.0),
        wspd10max_mps=c2(6.5),
        ustar_mps=c2(0.45),
        pblh_m=c2(1800.0),
        afwa_cape_jkg=c2(420.0),
        achfx_j_m2=c2(achfx),
        aclhf_j_m2=c2(achfx * 0.5),
        rainc_mm=c2(0.0),
        i_rainc=ci(0),
        rainnc_mm=c2(rainnc),
        i_rainnc=ci(i_rainnc),
        p_pert_pa=c3(0.0),
        pb_pa=CompMass3D(Slab3DF32.from_values(nz, ny, nx, pb_vals)),
        ph_stag_m2s2=CompStagZ3D(Slab3DF32.from_values(nz + 1, ny, nx, ph_vals)),
        phb_stag_m2s2=CompStagZ3D(Slab3DF32.from_values(nz + 1, ny, nx, phb_vals)),
        theta_pert_k=c3(12.0),
        qvapor_kgkg=c3(0.006),
        cldfra=c3(0.25),
        u_stag_mps=CompStagX3D(Slab3DF32.from_values(nz, ny, nx + 1, u_buf)),
        v_stag_mps=CompStagY3D(Slab3DF32.from_values(nz, ny + 1, nx, v_buf)),
        w_stag_mps=CompStagZ3D(Slab3DF32.from_values(nz + 1, ny, nx, w_vals)),
    )


class TestPostFrame(unittest.TestCase):
    def test_post_frame_signed_flux_fixed_300k_pressure_order_and_nan_guards(self) -> None:
        prod_id = sorted((ROOT / "config" / "products").glob("*.yaml"))[0].stem
        bundle = load_bundle(ROOT / "config", prod_id)
        dom = dataclasses.replace(
            bundle.domain,
            e_we=6,
            e_sn=5,
            bdy_trim_cells=1,
            e_vert=5,
            exported_levels_k=3,
            min_bl_levels_2km_agl=3,
        )
        # Even with base_temp_k=290.0, post_frame MUST invert T using fixed 300 K
        sci = dataclasses.replace(bundle.science, base_temp_k=290.0)
        f000 = _synthetic_step(
            1_000_000,
            "2026-11-12T00:00:00Z",
            4,
            5,
            4,
            achfx=1.0e7,
            rainnc=98.0,
            i_rainnc=0,
        )
        base0 = build_accum_baseline(f000, trim=1)

        f001 = _synthetic_step(
            1_003_600,
            "2026-11-12T01:00:00Z",
            4,
            5,
            4,
            achfx=1.072e7,
            rainnc=1.6,
            i_rainnc=1,
        )
        out1, base1 = post_frame(base0, f001, dom, sci)
        self.assertEqual(set(out1.fields_2d) | set(out1.fields_3d), set(WRF_ZARR_V3_FIELD_NAMES))
        self.assertGreater(validate_frame_columns(out1, dom), 0.0)
        self.assertAlmostEqual(out1.fields_2d["sensible_heat_flux_wm2"].at(0, 0), 200.0, delta=0.5)
        self.assertAlmostEqual(out1.fields_2d["precip_mm_hr"].at(0, 0), 3.6, places=2)
        expected_tk0 = (12.0 + 300.0) * ((85_000.0 / P0_PA) ** KAPPA_DRY)
        self.assertAlmostEqual(out1.fields_3d["temp_k"].at(0, 0, 0), expected_tk0, delta=0.02)

        # Negative nocturnal sensible heat flux across 3 h window without integer counters
        f004 = _synthetic_step(
            1_014_400,
            "2026-11-12T04:00:00Z",
            4,
            5,
            4,
            achfx=1.036e7,
            rainnc=10.6,
            i_rainnc=1,
        )
        out4, _ = post_frame(base1, f004, dom, sci)
        self.assertAlmostEqual(out4.fields_2d["sensible_heat_flux_wm2"].at(0, 0), -33.33, delta=0.5)
        self.assertAlmostEqual(out4.fields_2d["precip_mm_hr"].at(0, 0), 3.0, places=2)

        # Reject model-level pressure exceeding surface_pressure_pa (e.g. 900 hPa over 880 hPa)
        inv_p = [90_000.0 - 1_000.0 * k for k in range(4) for _ in range(20)]
        with self.assertRaises(ValueError):
            post_frame(
                base0,
                dataclasses.replace(f001, pb_pa=CompMass3D(Slab3DF32.from_values(4, 4, 5, inv_p))),
                dom,
                sci,
            )

        # Reject pressure increasing with height (900 -> 902 hPa)
        inc_p = [80_000.0 + 200.0 * k for k in range(4) for _ in range(20)]
        with self.assertRaises(ValueError):
            post_frame(
                base0,
                dataclasses.replace(f001, pb_pa=CompMass3D(Slab3DF32.from_values(4, 4, 5, inc_p))),
                dom,
                sci,
            )

        # Reject NaN in wind input
        bad_u = [8.0] * (4 * 4 * 6)
        bad_u[50] = float("nan")
        with self.assertRaises(ValueError):
            post_frame(
                base0,
                dataclasses.replace(
                    f001, u_stag_mps=CompStagX3D(Slab3DF32.from_values(4, 4, 6, bad_u))
                ),
                dom,
                sci,
            )


if __name__ == "__main__":
    unittest.main()
