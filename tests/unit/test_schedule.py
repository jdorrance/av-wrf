"""Unit tests for av_wrf.core.schedule loading shipped Armenia product & profiles."""

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
    parse_forcing_profile,
    parse_output_profile,
    parse_product_spec,
)
from av_wrf.core import (  # noqa: E402
    resolve_cycle_window,
    selected_lead_hours,
    validate_horizon_policy,
)
from av_wrf.runner import parse_declarative_yaml  # noqa: E402


class TestSchedule(unittest.TestCase):
    def test_shipped_product_horizon_cron_margin_and_trailing_windows(self) -> None:
        prod = parse_product_spec(
            parse_declarative_yaml(ROOT / "config" / "products" / "wrf-armenia-3km.yaml")
        )
        src = parse_forcing_profile(
            parse_declarative_yaml(
                ROOT / "config" / "profiles" / "sources" / f"{prod.primary_source_profile}.yaml"
            )
        )
        out = parse_output_profile(
            parse_declarative_yaml(
                ROOT / "config" / "profiles" / "outputs" / f"{prod.output_profile}.yaml"
            )
        )
        leads = selected_lead_hours(out)
        self.assertEqual(len(leads), 40)
        self.assertEqual(validate_horizon_policy(prod, src, out), 42)

        # Reject tight cron before p95 lag + 15m buffer (22:50 UTC vs 22:48 UTC ready)
        with self.assertRaises(ValueError):
            validate_horizon_policy(
                dataclasses.replace(prod, schedule_cron_utc="50 22 * * *"), src, out
            )
        with self.assertRaises(ValueError):
            validate_horizon_policy(
                dataclasses.replace(prod, min_horizon_hours=48), src, out
            )

        win = resolve_cycle_window(prod, out, "2026-11-12T00:00:00Z")
        self.assertEqual(win.forcing_cycle_utc, "2026-11-11T18:00:00Z")
        self.assertEqual(win.wrf_init_utc, "2026-11-11T18:00:00Z")
        self.assertEqual(win.window_starts_utc[0], "2026-11-12T00:00:00Z")
        self.assertEqual(win.valid_times_utc[0], "2026-11-12T01:00:00Z")
        # 1h -> 3h transition at f039 (index 36) must start at f036 (2026-11-13T12:00:00Z)
        self.assertEqual(win.lead_hours[36], 39)
        self.assertEqual(win.window_starts_utc[36], "2026-11-13T12:00:00Z")
        self.assertEqual(win.valid_times_utc[36], "2026-11-13T15:00:00Z")


if __name__ == "__main__":
    unittest.main()
