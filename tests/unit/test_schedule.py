"""Unit tests for av_wrf.core.schedule: horizon policy, UTC parsing, and live freshness gate."""

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
    is_cycle_live_fresh,
    parse_cycle_arg,
    resolve_cycle_window,
    selected_lead_hours,
    validate_horizon_policy,
)
from av_wrf.runner import load_bundle  # noqa: E402


class TestSchedule(unittest.TestCase):
    def test_all_shipped_products_horizon_cron_margin_freshness_and_cycle_parsing(self) -> None:
        cfg_root = ROOT / "config"
        prod_paths = sorted((cfg_root / "products").glob("*.yaml"))
        self.assertGreaterEqual(len(prod_paths), 1)

        for p_path in prod_paths:
            b = load_bundle(cfg_root, p_path.stem)
            prod, src, out = b.product, b.primary_source, b.output
            leads = selected_lead_hours(out)
            floor = validate_horizon_policy(prod, src, out)
            self.assertGreaterEqual(floor, prod.min_horizon_hours)

            iso_arg = f"2026-11-12T{prod.publish_origin_hour_utc:02d}:00:00Z"
            self.assertTrue(is_cycle_live_fresh(prod, out, iso_arg, "2026-11-12T04:00:00Z"))
            # Expired 2020 backfill is NOT live-fresh in November 2026
            self.assertFalse(
                is_cycle_live_fresh(
                    prod,
                    out,
                    f"2020-01-01T{prod.publish_origin_hour_utc:02d}:00:00Z",
                    "2026-11-12T04:00:00Z",
                )
            )
            # Far-future clock-skewed cycle is rejected for live pointer advancement
            self.assertFalse(is_cycle_live_fresh(prod, out, iso_arg, "2026-11-10T00:00:00Z"))

            with self.assertRaises(ValueError):
                validate_horizon_policy(
                    dataclasses.replace(prod, schedule_cron_utc="50 22 * * *"), src, out
                )
            with self.assertRaises(ValueError):
                validate_horizon_policy(
                    dataclasses.replace(prod, min_horizon_hours=out.map_end_hour), src, out
                )

            part_arg = f"20261112/{prod.publish_origin_hour_utc:02d}"
            self.assertEqual(parse_cycle_arg(iso_arg, prod), (iso_arg, part_arg))
            self.assertEqual(parse_cycle_arg(part_arg, prod), (iso_arg, part_arg))

            win = resolve_cycle_window(prod, out, part_arg)
            self.assertEqual(win.lead_hours, leads)
            self.assertEqual(win.window_starts_utc[0], iso_arg)

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
