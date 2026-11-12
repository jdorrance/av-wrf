"""Unit tests for av_wrf.core.schedule: local briefing deadline, cron margin, and freshness."""

from __future__ import annotations

import dataclasses
import sys
import unittest
from datetime import UTC, datetime, timedelta
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
from av_wrf.runner import load_bundle, validate_bundle  # noqa: E402


class TestSchedule(unittest.TestCase):
    def test_remaining_horizon_caps_live_age(self) -> None:
        cfg = ROOT / "config"
        b = load_bundle(cfg, next((cfg / "products").glob("*.yaml")).stem)
        age = b.output.map_end_hour - b.product.min_horizon_hours
        product = dataclasses.replace(
            b.product, max_cycle_age_hours=age + 2, region_tz="UTC", local_deadline_hhmm="18:00"
        )
        origin = datetime(2026, 11, 12, product.publish_origin_hour_utc, tzinfo=UTC)
        cutoff = origin + timedelta(hours=age)
        self.assertEqual(
            validate_horizon_policy(product, b.primary_source, b.output),
            product.min_horizon_hours,
        )
        self.assertTrue(
            is_cycle_live_fresh(product, b.output, origin.isoformat(), cutoff.isoformat())
        )
        self.assertFalse(
            is_cycle_live_fresh(
                product, b.output, origin.isoformat(), (cutoff + timedelta(seconds=1)).isoformat()
            )
        )

    def test_cron_requires_full_solver_margin_before_cycle_expiry(self) -> None:
        cfg = ROOT / "config"
        b = load_bundle(cfg, next((cfg / "products").glob("*.yaml")).stem)
        product = dataclasses.replace(b.product, local_deadline_hhmm="18:00")
        hour = (product.publish_origin_hour_utc + product.max_cycle_age_hours - 1) % 24
        validate_horizon_policy(
            dataclasses.replace(product, schedule_cron_utc=f"0 {hour} * * *"),
            b.primary_source,
            b.output,
        )
        with self.assertRaisesRegex(ValueError, "before the live cycle expires"):
            validate_horizon_policy(
                dataclasses.replace(product, schedule_cron_utc=f"1 {hour} * * *"),
                b.primary_source,
                b.output,
            )

    def test_all_shipped_products_horizon_deadline_freshness_and_cycle_parsing(self) -> None:
        cfg_root = ROOT / "config"
        prod_paths = sorted((cfg_root / "products").glob("*.yaml"))
        self.assertGreaterEqual(len(prod_paths), 1)

        for p_path in prod_paths:
            b = load_bundle(cfg_root, p_path.stem)
            prod, src, out = b.product, b.primary_source, b.output
            leads = selected_lead_hours(out)
            floor = validate_horizon_policy(prod, src, out)
            self.assertGreaterEqual(floor, prod.min_horizon_hours)

            # Invalid IANA timezone or unreachable local_deadline_hhmm is rejected
            with self.assertRaises(ValueError):
                validate_horizon_policy(
                    dataclasses.replace(prod, region_tz="Invalid/Zone"), src, out
                )
            with self.assertRaises(ValueError):
                validate_horizon_policy(
                    dataclasses.replace(prod, local_deadline_hhmm="04:05"),
                    src,
                    out,
                    publish_origin_utc=f"2026-11-12T{prod.publish_origin_hour_utc:02d}:00:00Z",
                )

            iso_arg = f"2026-11-12T{prod.publish_origin_hour_utc:02d}:00:00Z"
            self.assertTrue(is_cycle_live_fresh(prod, out, iso_arg, "2026-11-12T04:00:00Z"))
            self.assertFalse(
                is_cycle_live_fresh(
                    prod,
                    out,
                    f"2020-01-01T{prod.publish_origin_hour_utc:02d}:00:00Z",
                    "2026-11-12T04:00:00Z",
                )
            )
            self.assertFalse(is_cycle_live_fresh(prod, out, iso_arg, "2026-11-10T00:00:00Z"))

            with self.assertRaises(ValueError):
                hour = (prod.forcing_cycle_hour_utc + 1) % 24
                validate_horizon_policy(
                    dataclasses.replace(prod, schedule_cron_utc=f"0 {hour} * * *"), src, out
                )
            with self.assertRaises(ValueError):
                validate_horizon_policy(
                    dataclasses.replace(prod, min_horizon_hours=out.map_end_hour), src, out
                )

            part_arg = f"20261112/{prod.publish_origin_hour_utc:02d}"
            self.assertEqual(parse_cycle_arg(iso_arg, prod), (iso_arg, part_arg))
            self.assertEqual(parse_cycle_arg(iso_arg[:-1] + "z", prod), (iso_arg, part_arg))
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
        with self.assertRaisesRegex(ValueError, "Invalid ISO-8601 timestamp.*explicit Z/offset"):
            parse_iso_utc("2026-11-12T00:00:00xyz")

    def test_briefing_cutoff_follows_local_dst(self) -> None:
        cfg = ROOT / "config"
        b = load_bundle(cfg, next((cfg / "products").glob("*.yaml")).stem)
        product = dataclasses.replace(
            b.product, region_tz="America/New_York", local_deadline_hhmm="00:30"
        )
        for date, deadline_hour in (("2026-11-12", "05"), ("2026-07-12", "04")):
            origin = f"{date}T00:00:00Z"
            deadline = f"{date}T{deadline_hour}:30:00Z"
            after = f"{date}T{deadline_hour}:30:01Z"
            self.assertTrue(is_cycle_live_fresh(product, b.output, origin, deadline))
            self.assertFalse(is_cycle_live_fresh(product, b.output, origin, after))

    def test_config_check_catches_a_summer_only_briefing_failure(self) -> None:
        cfg = ROOT / "config"
        b = load_bundle(cfg, next((cfg / "products").glob("*.yaml")).stem)
        product = dataclasses.replace(
            b.product,
            region_tz="America/New_York",
            local_deadline_hhmm="20:01",
            forcing_cycle_hour_utc=18,
            publish_origin_hour_utc=0,
            schedule_cron_utc="15 23 * * *",
        )
        changed = dataclasses.replace(b, product=product)
        validate_bundle(changed, publish_origin_utc="20260101/00")
        with self.assertRaisesRegex(ValueError, "before briefing deadline"):
            validate_bundle(changed)


if __name__ == "__main__":
    unittest.main()
