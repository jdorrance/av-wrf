"""Offline cardinality contract for emitted namelists against pinned WRF sources."""

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from unittest import TestCase

from av_wrf.contracts import BuildManifest, RunLock
from av_wrf.core import compile_wrf_run_namelist
from av_wrf.core.namelist import NamelistSections
from av_wrf.runner import load_bundle

ROOT = Path(__file__).resolve().parents[2]
WRF_COMMIT = "06d4240ae989cc3e50af412bb472df3d9048783c"
# Snapshot of rconfig columns 3/4/5, independently extracted from the pinned Registry.
# https://github.com/wrf-model/WRF/tree/06d4240ae989cc3e50af412bb472df3d9048783c/Registry
# Sources: Registry.EM_COMMON, registry.afwa, registry.diags, registry.hyb_coord,
# registry.io_boilerplate, registry.lake, registry.noahmp, registry.solar_fields.
# Quilt scalars are declared outside the Registry in frame/module_io_quilt_old.F.
_SCALAR = {
    "time_control": """
        interval_seconds run_days run_hours run_minutes run_seconds io_form_history
        io_form_restart io_form_input io_form_boundary ignore_iofields_warning
    """,
    "domains": """
        time_step max_dom num_metgrid_levels num_metgrid_soil_levels use_sh_qv feedback
        smooth_option p_top_requested auto_levels_opt dzbot dzstretch_s dzstretch_u max_dz
        use_adaptive_time_step step_to_output_time adaptation_domain
    """,
    "physics": """
        shadlen ysu_topdown_pblmix icloud icloud_bl bucket_mm force_read_thompson
        write_thompson_tables
    """,
    "noah_mp": "opt_runsrf opt_runsub",
    "dynamics": "use_theta_m base_temp hybrid_opt etac w_damping damp_opt",
    "bdy_control": "spec_bdy_width spec_zone relax_zone",
    "namelist_quilt": "nio_tasks_per_group nio_groups",
}
_PER_DOMAIN = {
    "time_control": """
        start_year start_month start_day start_hour end_year end_month end_day end_hour
        history_begin_h history_interval frames_per_outfile input_from_file iofields_filename
    """,
    "domains": """
        e_we e_sn e_vert dx dy grid_id parent_id i_parent_start j_parent_start
        parent_grid_ratio parent_time_step_ratio target_cfl target_hcfl max_step_increase_pct
        starting_time_step min_time_step max_time_step
    """,
    "physics": """
        mp_physics cu_physics ra_lw_physics ra_sw_physics radt slope_rad topo_shading
        sf_sfclay_physics sf_surface_physics bl_pbl_physics bl_mynn_edmf topo_wind
        sf_lake_physics shinhong_scu_mixing shinhong_nonlocal_flux use_lakedepth
        lakedepth_default lake_min_elev
    """,
    "dynamics": """
        diff_opt km_opt diff_6th_opt diff_6th_factor diff_6th_slopeopt diff_6th_thresh epssm
        zdamp dampcoef gwd_opt gwd_if_nonhyd
    """,
    "afwa": "afwa_diag_opt afwa_buoy_opt",
    "bdy_control": "specified nested",
}


def _compiled_cases() -> Iterator[NamelistSections]:
    for path in sorted((ROOT / "config/products").glob("*.yaml")):
        bundle = load_bundle(ROOT / "config", path.stem)
        domain, science = bundle.domain, bundle.science
        lock = RunLock(
            product_id=bundle.product.product_id,
            model_id=bundle.product.model_id,
            cycle_partition="20261112/00",
            attempt_id="registry",
            forcing_cycle_utc="2026-11-11T18:00:00Z",
            wrf_init_utc="2026-11-11T18:00:00Z",
            publish_origin_utc="2026-11-12T00:00:00Z",
            spinup_hours=6,
            lead_hours=(1, 2, 16),
            forcing_profile_id=bundle.primary_source.profile_id,
            domain_profile_id=domain.profile_id,
            science_profile_id=science.profile_id,
            output_profile_id=bundle.output.profile_id,
            config_sha256="fixture",
            geog_bundle_sha256="fixture",
            build_manifest=BuildManifest("fixture", "fixture", "fixture", (), "", ""),
            mpi_ranks=1,
        )
        variants = (
            science,
            replace(science, use_adaptive_time_step=False),
            replace(science, mp_physics=6, sf_lake_physics=0, icloud_bl=0),
        )
        for source in (bundle.primary_source, bundle.candidate_source):
            for variant in variants:
                for for_real in (False, True):
                    yield compile_wrf_run_namelist(
                        domain,
                        variant,
                        replace(lock, forcing_profile_id=source.profile_id),
                        source,
                        for_real=for_real,
                    )


class TestNamelistRegistry(TestCase):
    def test_all_emitted_keys_match_pinned_registry_cardinality(self) -> None:
        self.assertIn(WRF_COMMIT, (ROOT / "docker/build-wrf.sh").read_text())
        shapes = {
            (section, key): per_domain
            for per_domain, groups in ((False, _SCALAR), (True, _PER_DOMAIN))
            for section, keys in groups.items()
            for key in keys.split()
        }
        self.assertEqual(
            len(shapes), sum(len(s.split()) for d in (_SCALAR, _PER_DOMAIN) for s in d.values())
        )
        seen: set[tuple[str, str]] = set()
        domains_seen: set[int] = set()
        for sections in _compiled_cases():
            max_dom = sections["domains"]["max_dom"]
            self.assertIsInstance(max_dom, int)
            assert isinstance(max_dom, int)
            domains_seen.add(max_dom)
            for section, options in sections.items():
                for key, value in options.items():
                    identity = (section, key)
                    self.assertIn(identity, shapes, f"Unreviewed WRF namelist key {identity}")
                    seen.add(identity)
                    with self.subTest(domain_count=max_dom, field=identity):
                        if shapes[identity] and max_dom > 1:
                            self.assertIsInstance(value, tuple)
                            assert isinstance(value, tuple)
                            self.assertEqual(len(value), max_dom)
                        else:
                            self.assertNotIsInstance(value, tuple)
        self.assertEqual(domains_seen, {1, 2})
        self.assertEqual(seen, set(shapes), "Remove unused snapshot keys or cover their branch")
