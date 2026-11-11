"""Unit tests for av_wrf.core.schedule across all configured products and UTC parsers."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.contracts import parse_iso_utc  # noqa: E402
from av_wrf.core import (  # noqa: E402
    parse_cycle_arg,
    resolve_cycle_window,
    selected_lead_hours,
    validate_horizon_policy,
)
from av_wrf.runner import load_bundle  # noqa: E402


class TestSchedule(unittest.TestCase):
    def test_all_shipped_products_horizon_cron_margin_and_cycle_parsing(self) -> None:
        cfg_root = ROOT / "config"
        prod_paths = sorted((cfg_root / "products").glob("*.yaml"))
        self.assertGreaterEqual(len(prod_paths), 1)

        for p_path in prod_paths:
            b = load_bundle(cfg_root, p_path.stem)
            prod, src, out = b.product, b.primary_source, b.output
            leads = selected_lead_hours(out)
            floor = validate_horizon_policy(prod, src, out)
            self.assertGreaterEqual(floor, prod.min_horizon_hours)

            with self.assertRaises(ValueError):
                validate_horizon_policy(
                    dataclasses.replace(prod, schedule_cron_utc="50 22 * * *"), src, out
                )
            with self.assertRaises(ValueError):
                validate_horizon_policy(
                    dataclasses.replace(prod, min_horizon_hours=out.map_end_hour), src, out
                )

            iso_arg = f"2026-11-12T{prod.publish_origin_hour_utc:02d}:00:00Z"
            part_arg = f"20261112/{prod.publish_origin_hour_utc:02d}"
            self.assertEqual(parse_cycle_arg(iso_arg, prod), (iso_arg, part_arg))
            self.assertEqual(parse_cycle_arg(part_arg, prod), (iso_arg, part_arg))

            win = resolve_cycle_window(prod, out, part_arg)
            self.assertEqual(win.lead_hours, leads)
            self.assertEqual(win.window_starts_utc[0], iso_arg)
            if len(leads) == 40:
                self.assertEqual(win.lead_hours[36], 39)
                self.assertEqual(win.window_starts_utc[36], "2026-11-13T12:00:00Z")
                self.assertEqual(win.valid_times_utc[36], "2026-11-13T15:00:00Z")

            # Reject timezone-naive ISO strings, bad partitions, and wrong UTC hours
            for bad_cycle in (
                "2026-11-12T00:00:00",
                "20261112/bad",
                "bad/00",
                f"2026-11-12T{(prod.publish_origin_hour_utc + 1) % 24:02d}:00:00Z",
            ):
                with self.assertRaises(ValueError):
                    parse_cycle_arg(bad_cycle, prod)

        with self.assertRaises(ValueError):
            parse_iso_utc("2026-11-12T00:00:00")


if __name__ == "__main__":
    unittest.main()
