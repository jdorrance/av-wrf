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
    compile_geogrid_namelist_sections,
    compile_wps_namelist_sections,
    compile_wrf_namelist_sections,
    compile_wrf_run_namelist,
    render_fortran_namelist,
    required_forcing_forecast_hours,
)
from av_wrf.runner import load_bundle  # noqa: E402

PROFILE_TO_NAMELIST_KEY: dict[str, tuple[str, str]] = {
    "history_interval_min": ("time_control", "history_interval"),
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
    "afwa_buoy_opt": ("afwa", "afwa_buoy_opt"),
    "spec_bdy_width": ("bdy_control", "spec_bdy_width"),
    "spec_zone": ("bdy_control", "spec_zone"),
    "relax_zone": ("bdy_control", "relax_zone"),
}


class TestNamelist(unittest.TestCase):
    def test_exhaustive_science_profile_real_exe_fields_and_wrf_48_guards(self) -> None:
        prod_id = sorted((ROOT / "config" / "products").glob("*.yaml"))[0].stem
        bundle = load_bundle(ROOT / "config", prod_id)
        dom, sci, src, cand = (
            bundle.domain,
            bundle.science,
            bundle.primary_source,
            bundle.candidate_source,
        )

        all_fields = {f.name for f in dataclasses.fields(ScienceProfile)}
        self.assertEqual(
            all_fields - {"profile_id", "wrf_version", "wps_version"},
            set(PROFILE_TO_NAMELIST_KEY.keys()),
        )
        compiled = compile_wrf_namelist_sections(dom, sci, src)
        for attr, (sec, key) in PROFILE_TO_NAMELIST_KEY.items():
            self.assertEqual(compiled[sec][key], getattr(sci, attr))
        self.assertEqual(compiled["time_control"]["interval_seconds"], src.interval_seconds)
        cand_compiled = compile_wrf_namelist_sections(dom, sci, cand)
        self.assertEqual(cand_compiled["time_control"]["interval_seconds"], cand.interval_seconds)
        self.assertNotIn("prec_acc_dt", compiled["time_control"])
        self.assertIsInstance(compiled["physics"]["shinhong_scu_mixing"], bool)
        self.assertEqual(compiled["afwa"]["afwa_buoy_opt"], 1)

        override = compile_wrf_namelist_sections(
            dom, sci, src, metgrid_levels=38, metgrid_soil_levels=4
        )
        self.assertEqual(override["domains"]["num_metgrid_levels"], 38)
        with self.assertRaisesRegex(ValueError, "at least two"):
            compile_wrf_namelist_sections(dom, sci, src, metgrid_soil_levels=1)

        for bad in (
            dataclasses.replace(sci, opt_runsrf=5),
            dataclasses.replace(sci, gwd_opt=1, gwd_if_nonhyd=True),
            dataclasses.replace(sci, shinhong_scu_mixing=True, shinhong_nonlocal_flux=True),
            dataclasses.replace(sci, km_opt=5, bl_pbl_physics=0, sf_sfclay_physics=2),
            dataclasses.replace(sci, km_opt=5),
            dataclasses.replace(sci, bl_pbl_physics=10, sf_sfclay_physics=1),
            dataclasses.replace(sci, bl_pbl_physics=1, sf_sfclay_physics=10),
            dataclasses.replace(sci, time_step_s=24),
            dataclasses.replace(sci, afwa_buoy_opt=0),
            dataclasses.replace(sci, base_temp_k=100.0),
            dataclasses.replace(sci, hybrid_opt=0),
            dataclasses.replace(sci, history_interval_min=45),
            dataclasses.replace(sci, wrf_version="99"),
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
        custom_dom = dataclasses.replace(dom, dem_source="cop_glo30", landuse_source="modis_15s")
        wps_3h = compile_wps_namelist_sections(custom_dom, sci, lock, cand)
        self.assertEqual(wps_3h["share"]["end_date"], "2026-11-12_18:00:00")
        self.assertEqual(wps_3h["share"]["interval_seconds"], 10_800)
        self.assertEqual(wps_3h["geogrid"]["geog_data_res"], "modis_15s+cop_glo30+default")
        self.assertEqual(required_forcing_forecast_hours(lock, src), tuple(range(23)))
        self.assertEqual(required_forcing_forecast_hours(lock, cand), tuple(range(0, 25, 3)))
        real = compile_wrf_run_namelist(dom, sci, lock, cand, for_real=True)["time_control"]
        wrf = compile_wrf_run_namelist(dom, sci, lock, cand)["time_control"]
        self.assertEqual(real["end_hour"], 18)
        self.assertEqual(wrf["end_hour"], 16)
        self.assertEqual(wrf["run_hours"], 22)

        wrf_txt = render_fortran_namelist(compile_wrf_run_namelist(dom, sci, lock, src))
        self.assertNotIn("nwp_diagnostics", wrf_txt)
        self.assertNotIn("afwa_severe_opt", wrf_txt)
        self.assertNotIn("bucket_J", wrf_txt)
        self.assertIn("shinhong_scu_mixing = .false.,", wrf_txt)
        self.assertIn("afwa_buoy_opt = 1,", wrf_txt)
        self.assertIn("&namelist_quilt\n nio_tasks_per_group = 0,\n nio_groups = 1,", wrf_txt)
        self.assertNotIn("prec_acc_dt", wrf_txt)

    def test_custom_geography_resolution_precedes_default_for_each_field(self) -> None:
        config = ROOT / "config"
        dom = load_bundle(config, next((config / "products").glob("*.yaml")).stem).domain
        for landuse, dem in (("custom", "default"), ("default", "custom"), ("custom", "custom")):
            domain = dataclasses.replace(dom, landuse_source=landuse, dem_source=dem)
            sections = compile_geogrid_namelist_sections(domain)
            self.assertEqual(sections["geogrid"]["geog_data_res"], "custom+default")
            self.assertNotIn("start_date", sections["share"])

    def test_wide_lambert_grid_limits_timestep_at_its_corners(self) -> None:
        product = next((ROOT / "config" / "products").glob("*.yaml")).stem
        bundle = load_bundle(ROOT / "config", product)
        domain = dataclasses.replace(
            bundle.domain,
            e_we=401,
            e_sn=101,
            dx_m=12_000,
            dy_m=12_000,
            center_lat_deg=55.0,
            std_parallel_1_deg=54.0,
            std_parallel_2_deg=56.0,
        )
        with self.assertRaisesRegex(ValueError, "exceeds 6\\*dx_phys"):
            compile_wrf_namelist_sections(
                domain, dataclasses.replace(bundle.science, time_step_s=71), bundle.primary_source
            )


if __name__ == "__main__":
    unittest.main()
