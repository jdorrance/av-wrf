"""Unit tests for av_wrf.core.namelist verifying exhaustive ScienceProfile emission."""

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
    ScienceProfile,
    parse_domain_profile,
    parse_science_profile,
)
from av_wrf.core import (  # noqa: E402
    NON_NAMELIST_METADATA_FIELDS,
    compile_wrf_namelist_sections,
)
from av_wrf.runner import parse_declarative_yaml  # noqa: E402

PROFILE_TO_NAMELIST_KEY: dict[str, tuple[str, str]] = {
    "interval_seconds": ("time_control", "interval_seconds"),
    "history_interval_min": ("time_control", "history_interval"),
    "prec_acc_dt_min": ("time_control", "prec_acc_dt"),
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
    "nwp_diagnostics": ("physics", "nwp_diagnostics"),
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
    "spec_bdy_width": ("bdy_control", "spec_bdy_width"),
    "spec_zone": ("bdy_control", "spec_zone"),
    "relax_zone": ("bdy_control", "relax_zone"),
}


class TestNamelist(unittest.TestCase):
    def test_exhaustive_science_profile_mapping_and_wrf_48_guards(self) -> None:
        dom = parse_domain_profile(
            parse_declarative_yaml(
                ROOT / "config" / "profiles" / "domains" / "armenia-3km-v1.yaml"
            )
        )
        sci = parse_science_profile(
            parse_declarative_yaml(
                ROOT / "config" / "profiles" / "science" / "convection-permitting-3km-v1.yaml"
            )
        )
        all_fields = {f.name for f in dataclasses.fields(ScienceProfile)}
        self.assertEqual(
            all_fields - NON_NAMELIST_METADATA_FIELDS,
            set(PROFILE_TO_NAMELIST_KEY.keys()),
        )
        compiled = compile_wrf_namelist_sections(dom, sci)
        for attr, (sec, key) in PROFILE_TO_NAMELIST_KEY.items():
            self.assertEqual(compiled[sec][key], getattr(sci, attr))

        for bad in (
            dataclasses.replace(sci, opt_runsrf=5),
            dataclasses.replace(sci, gwd_opt=1, gwd_if_nonhyd=True),
            dataclasses.replace(sci, shinhong_scu_mixing=1, shinhong_nonlocal_flux=True),
            dataclasses.replace(sci, time_step_s=24),
            dataclasses.replace(sci, afwa_severe_opt=0),
            dataclasses.replace(sci, base_temp_k=100.0),
        ):
            with self.assertRaises(ValueError):
                compile_wrf_namelist_sections(dom, bad)


if __name__ == "__main__":
    unittest.main()
