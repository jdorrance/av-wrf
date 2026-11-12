"""Typed profile defaults remain optional while required and unknown keys fail closed."""

from __future__ import annotations

from dataclasses import MISSING, fields
from pathlib import Path
from unittest import TestCase

from av_wrf.contracts import (
    DomainProfile,
    ScienceProfile,
    parse_domain_profile,
    parse_forcing_profile,
    parse_science_profile,
)
from av_wrf.runner import parse_declarative_yaml

CONFIG = Path(__file__).resolve().parents[2] / "config" / "profiles"


class TestConfigParsers(TestCase):
    def test_omitted_domain_options_use_typed_defaults(self) -> None:
        raw = parse_declarative_yaml(next((CONFIG / "domains").glob("*.yaml")))
        for field in fields(DomainProfile):
            if field.default is not MISSING:
                raw.pop(field.name, None)
        domain = parse_domain_profile(raw)
        self.assertEqual(domain.parent_grid_ratio, 1)
        self.assertEqual(domain.parent_halo_cells, 0)
        self.assertEqual(domain.nest_trigger_ratio, 3.5)
        self.assertEqual(domain.lai_source, "modis_lai")
        self.assertEqual(domain.varsso_source, "30s")

    def test_omitted_science_options_use_typed_defaults(self) -> None:
        raw = parse_declarative_yaml(next((CONFIG / "science").glob("*.yaml")))
        for field in fields(ScienceProfile):
            if field.default is not MISSING:
                raw.pop(field.name, None)
        science = parse_science_profile(raw)
        self.assertFalse(science.use_adaptive_time_step)
        self.assertEqual(science.bl_mynn_edmf, 0)
        self.assertEqual(science.icloud, 1)
        self.assertEqual(science.min_time_step_s, 6)
        self.assertEqual(science.max_time_step_s, 17)
        self.assertEqual(science.lakedepth_default_m, 50.0)

    def test_explicit_options_override_defaults_and_reject_wrong_types(self) -> None:
        domain = parse_declarative_yaml(next((CONFIG / "domains").glob("*.yaml")))
        science = parse_declarative_yaml(next((CONFIG / "science").glob("*.yaml")))
        self.assertEqual(
            parse_domain_profile({**domain, "parent_grid_ratio": 3}).parent_grid_ratio, 3
        )
        self.assertEqual(
            parse_science_profile({**science, "max_time_step_s": 21}).max_time_step_s, 21
        )
        with self.assertRaisesRegex(TypeError, "Expected int for 'parent_grid_ratio'"):
            parse_domain_profile({**domain, "parent_grid_ratio": True})
        with self.assertRaisesRegex(TypeError, "Expected bool for 'use_adaptive_time_step'"):
            parse_science_profile({**science, "use_adaptive_time_step": "false"})

    def test_required_and_unknown_keys_remain_errors(self) -> None:
        for folder, parser, required in (
            ("domains", parse_domain_profile, "dx_m"),
            ("science", parse_science_profile, "time_step_s"),
        ):
            raw = parse_declarative_yaml(next((CONFIG / folder).glob("*.yaml")))
            with self.subTest(folder=folder):
                with self.assertRaisesRegex(ValueError, f"missing required keys: {required}"):
                    parser({key: value for key, value in raw.items() if key != required})
                with self.assertRaisesRegex(ValueError, "unknown keys: misspelled_option"):
                    parser({**raw, "misspelled_option": 1})

    def test_forcing_specific_humidity_option_defaults_and_validates(self) -> None:
        raw = parse_declarative_yaml(next((CONFIG / "sources").glob("*.yaml")))
        raw.pop("use_sh_qv", None)
        self.assertFalse(parse_forcing_profile(raw).use_sh_qv)
        self.assertTrue(parse_forcing_profile({**raw, "use_sh_qv": True}).use_sh_qv)
        with self.assertRaisesRegex(TypeError, "Expected bool for 'use_sh_qv'"):
            parse_forcing_profile({**raw, "use_sh_qv": 1})
