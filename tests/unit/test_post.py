"""Unit tests for av_wrf.core.post stateful post_frame across f000, restart, and 3h tail."""

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
    CompMass2D,
    CompMass3D,
    CompStagX3D,
    CompStagY3D,
    CompStagZ3D,
    Slab2DF32,
    Slab3DF32,
    WRF_ZARR_V3_FIELD_NAMES,
    parse_domain_profile,
    parse_science_profile,
)
from av_wrf.core import (  # noqa: E402
    RawWrfStepInput,
    build_accum_baseline,
    post_frame,
)
from av_wrf.runner import parse_declarative_yaml  # noqa: E402


def _synthetic_step(
    unix_s: int,
    iso_utc: str,
    ny: int,
    nx: int,
    nz: int,
    *,
    achfx: float,
    i_achfx: int,
    rainnc: float,
    i_rainnc: int,
) -> RawWrfStepInput:
    n2 = ny * nx

    def c2(val: float) -> CompMass2D:
        return CompMass2D(Slab2DF32.from_values(ny, nx, [val] * n2))

    def c3(val: float) -> CompMass3D:
        return CompMass3D(Slab3DF32.from_values(nz, ny, nx, [val] * (nz * n2)))

    def sz(val: float) -> CompStagZ3D:
        vals = [val * (k + 1) for k in range(nz + 1) for _ in range(n2)]
        return CompStagZ3D(Slab3DF32.from_values(nz + 1, ny, nx, vals))

    pub_cells = (ny - 2) * (nx - 2)
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
        i_achfx=(i_achfx,) * pub_cells,
        aclhf_j_m2=c2(achfx * 0.5),
        i_aclhf=(0,) * pub_cells,
        rainc_mm=c2(0.0),
        i_rainc=(0,) * pub_cells,
        rainnc_mm=c2(rainnc),
        i_rainnc=(i_rainnc,) * pub_cells,
        p_pert_pa=c3(500.0),
        pb_pa=c3(80_000.0),
        ph_stag_m2s2=sz(100.0),
        phb_stag_m2s2=sz(2000.0),
        theta_pert_k=c3(12.0),
        qvapor_kgkg=c3(0.006),
        cldfra=c3(0.25),
        u_stag_mps=CompStagX3D(Slab3DF32.from_values(nz, ny, nx + 1, u_buf)),
        v_stag_mps=CompStagY3D(Slab3DF32.from_values(nz, ny + 1, nx, v_buf)),
        w_stag_mps=sz(0.2),
    )


class TestPostFrame(unittest.TestCase):
    def test_post_frame_f000_baseline_restart_and_1h_to_3h_tail(self) -> None:
        dom = dataclasses.replace(
            parse_domain_profile(
                parse_declarative_yaml(
                    ROOT / "config" / "profiles" / "domains" / "armenia-3km-v1.yaml"
                )
            ),
            e_we=6,
            e_sn=5,
            bdy_trim_cells=1,
            e_vert=5,
            exported_levels_k=3,
        )
        sci = parse_science_profile(
            parse_declarative_yaml(
                ROOT / "config" / "profiles" / "science" / "convection-permitting-3km-v1.yaml"
            )
        )
        f000 = _synthetic_step(
            1_000_000,
            "2026-11-12T00:00:00Z",
            4,
            5,
            4,
            achfx=9.5e8,
            i_achfx=0,
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
            achfx=0.22e8,
            i_achfx=1,
            rainnc=1.6,
            i_rainnc=1,
        )
        out1, base1 = post_frame(base0, f001, dom, sci)
        exported = set(out1.fields_2d) | set(out1.fields_3d)
        self.assertEqual(exported, set(WRF_ZARR_V3_FIELD_NAMES))
        self.assertAlmostEqual(
            out1.fields_2d["sensible_heat_flux_wm2"].at(0, 0), 20_000.0, delta=2.0
        )
        self.assertAlmostEqual(out1.fields_2d["precip_mm_hr"].at(0, 0), 3.6, places=2)
        self.assertGreater(out1.fields_2d["gust_10m_mps"].at(0, 0), 6.5)

        f004 = _synthetic_step(
            1_014_400,
            "2026-11-12T04:00:00Z",
            4,
            5,
            4,
            achfx=0.2164e8,
            i_achfx=1,
            rainnc=10.6,
            i_rainnc=1,
        )
        out4, _ = post_frame(base1, f004, dom, sci)
        self.assertEqual(out4.window_end_unix_s - out4.window_start_unix_s, 10_800)
        self.assertAlmostEqual(
            out4.fields_2d["sensible_heat_flux_wm2"].at(0, 0), -33.33, delta=0.5
        )
        self.assertAlmostEqual(out4.fields_2d["precip_mm_hr"].at(0, 0), 3.0, places=2)


if __name__ == "__main__":
    unittest.main()
