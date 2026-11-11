"""Unit tests for av_wrf.core.namelist verifying WRF v4.8 Registry placement and types."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.contracts import BuildManifest, RunLock, ScienceProfile  # noqa: E402
from av_wrf.core import (  # noqa: E402
    NON_NAMELIST_METADATA_FIELDS,
    compile_wps_namelist_sections,
    compile_wrf_namelist_sections,
    compile_wrf_run_namelist,
    render_fortran_namelist,
)
from av_wrf.runner import load_bundle  # noqa: E402

PROFILE_TO_NAMELIST_KEY: dict[str, tuple[str, str]] = {
    "interval_seconds": ("time_control", "interval_seconds"),
    "history_interval_min": ("time_control", "history_interval"),
    "nwp_diagnostics": ("time_control", "nwp_diagnostics"),
    "time_step_s": ("domains", "time_step"),
    "mp_physics": ("physics", "mp_physics"),
    "cu_physics": ("physics", "cu_physics"),
    "ra_lw_physics": ("physics", "ra_lw_physics"),
    "ra_sw_physics": ("physics", "ra_sw_physics"),
    "radt_min": ("physics", "radt"),
    "slope_rad": ("physics", "slope_rad"),
    "topo_shading": ("physics", "topo_shading"),
    "shadlen_m": ("physics", "shadlen"),
    "sf_sfclay_physics": ("physics", "sf_sfclay_physics"),
    "sf_surface_physics": ("physics", "sf_surface_physics"),
    "bl_pbl_physics": ("physics", "bl_pbl_physics"),
    "topo_wind": ("physics", "topo_wind"),
    "ysu_topdown_pblmix": ("physics", "ysu_topdown_pblmix"),
    "shinhong_scu_mixing": ("physics", "shinhong_scu_mixing"),
    "shinhong_nonlocal_flux": ("physics", "shinhong_nonlocal_flux"),
    "bucket_mm": ("physics", "bucket_mm"),
    "bucket_j": ("physics", "bucket_J"),
    "opt_runsrf": ("noah_mp", "opt_runsrf"),
    "opt_runsub": ("noah_mp", "opt_runsub"),
    "use_theta_m": ("dynamics", "use_theta_m"),
    "base_temp_k": ("dynamics", "base_temp"),
    "hybrid_opt": ("dynamics", "hybrid_opt"),
    "etac": ("dynamics", "etac"),
    "diff_opt": ("dynamics", "diff_opt"),
    "km_opt": ("dynamics", "km_opt"),
    "diff_6th_opt": ("dynamics", "diff_6th_opt"),
    "diff_6th_factor": ("dynamics", "diff_6th_factor"),
    "epssm": ("dynamics", "epssm"),
    "w_damping": ("dynamics", "w_damping"),
    "damp_opt": ("dynamics", "damp_opt"),
    "zdamp_m": ("dynamics", "zdamp"),
    "dampcoef": ("dynamics", "dampcoef"),
    "gwd_opt": ("dynamics", "gwd_opt"),
    "gwd_if_nonhyd": ("dynamics", "gwd_if_nonhyd"),
    "afwa_diag_opt": ("afwa", "afwa_diag_opt"),
    "afwa_severe_opt": ("afwa", "afwa_severe_opt"),
    "afwa_buoy_opt": ("afwa", "afwa_buoy_opt"),
    "spec_bdy_width": ("bdy_control", "spec_bdy_width"),
    "spec_zone": ("bdy_control", "spec_zone"),
    "relax_zone": ("bdy_control", "relax_zone"),
}


class TestNamelist(unittest.TestCase):
    def test_exhaustive_science_profile_real_exe_fields_and_wrf_48_guards(self) -> None:
        prod_id = sorted((ROOT / "config" / "products").glob("*.yaml"))[0].stem
        bundle = load_bundle(ROOT / "config", prod_id)
        dom, sci, src = bundle.domain, bundle.science, bundle.primary_source

        all_fields = {f.name for f in dataclasses.fields(ScienceProfile)}
        self.assertEqual(
            all_fields - NON_NAMELIST_METADATA_FIELDS,
            set(PROFILE_TO_NAMELIST_KEY.keys()),
        )
        compiled = compile_wrf_namelist_sections(dom, sci, src)
        for attr, (sec, key) in PROFILE_TO_NAMELIST_KEY.items():
            self.assertEqual(compiled[sec][key], getattr(sci, attr))
        self.assertNotIn("prec_acc_dt", compiled["time_control"])
        self.assertNotIn("nwp_diagnostics", compiled["physics"])
        self.assertIsInstance(compiled["physics"]["shinhong_scu_mixing"], bool)
        self.assertEqual(compiled["afwa"]["afwa_buoy_opt"], 1)

        override = compile_wrf_namelist_sections(
            dom, sci, src, metgrid_levels=38, metgrid_soil_levels=4
        )
        self.assertEqual(override["domains"]["num_metgrid_levels"], 38)

        for bad in (
            dataclasses.replace(sci, opt_runsrf=5),
            dataclasses.replace(sci, gwd_opt=1, gwd_if_nonhyd=True),
            dataclasses.replace(sci, shinhong_scu_mixing=True, shinhong_nonlocal_flux=True),
            dataclasses.replace(sci, time_step_s=24),
            dataclasses.replace(sci, afwa_severe_opt=0),
            dataclasses.replace(sci, afwa_buoy_opt=0),
            dataclasses.replace(sci, base_temp_k=100.0),
        ):
            with self.assertRaises(ValueError):
                compile_wrf_namelist_sections(dom, bad, src)

        with self.assertRaises(ValueError):
            compile_wrf_namelist_sections(dataclasses.replace(dom, e_we=100), sci, src)

        lock = RunLock(
            product_id="wrf-synth-3km",
            model_id="wrf-synth-3km",
            cycle_partition="20261112/00",
            attempt_id="att-01",
            forcing_cycle_utc="2026-11-11T18:00:00Z",
            wrf_init_utc="2026-11-11T18:00:00Z",
            publish_origin_utc="2026-11-12T00:00:00Z",
            spinup_hours=6,
            lead_hours=(1, 2, 16),
            forcing_profile_id=src.profile_id,
            domain_profile_id=dom.profile_id,
            science_profile_id=sci.profile_id,
            output_profile_id=bundle.output.profile_id,
            config_sha256="abc",
            geog_bundle_sha256="def",
            build_manifest=BuildManifest("sha256:1", "w", "p", (), "gf", "mpi"),
            mpi_ranks=4,
        )
        # 3-hourly boundary cadence on 22h total span (6h + 16h) brackets up to f024 (18Z + 24h)
        wps_3h = compile_wps_namelist_sections(dom, sci, lock, interval_seconds=10_800)
        self.assertEqual(wps_3h["share"]["end_date"], "2026-11-12_18:00:00")

        wrf_txt = render_fortran_namelist(compile_wrf_run_namelist(dom, sci, lock, src))
        self.assertIn("nwp_diagnostics = 1,", wrf_txt)
        self.assertIn("shinhong_scu_mixing = .false.,", wrf_txt)
        self.assertIn("afwa_buoy_opt = 1,", wrf_txt)
        self.assertNotIn("prec_acc_dt", wrf_txt)


if __name__ == "__main__":
    unittest.main()
