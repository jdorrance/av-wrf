"""Check DWD native inventory and normalized ICON-EU GRIB2 admission."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import httpx

from av_wrf.contracts import BuildManifest
from av_wrf.runner.forcing_icon import _REQUIRED, _SOIL_TEMP, _SOIL_TEMP_SELECTORS, ICON_EU
from av_wrf.runner.grib import GRIB_FIELD_KEYS
from av_wrf.runner.icon_acquire import _download, _read_plan
from av_wrf.runner.stages import plan_product_cycle, prep_cycle_namelists
from av_wrf.runner.store import LocalFileStore
from tests.integration import bundle_for_recipe

CONFIG = Path(__file__).resolve().parents[2] / "config"
CYCLE = datetime(2026, 11, 11, 18, tzinfo=UTC)
_FRAME = b"GRIB\x00\x00\x00\x02" + (20).to_bytes(8, "big") + b"7777"


def _messages() -> list[dict[str, object]]:
    metadata = {
        "edition": 2,
        "centre": 78,
        "generatingProcessIdentifier": 2,
        "productDefinitionTemplateNumber": 0,
        "dataDate": 20261111,
        "dataTime": 1800,
        "validityDate": 20261112,
        "validityTime": 100,
        "iDirectionIncrementInDegrees": 0.0625,
        "jDirectionIncrementInDegrees": 0.0625,
        "iScansNegatively": 0,
        "jPointsAreConsecutive": 0,
        "alternativeRowScanning": 0,
        "gridType": "regular_ll",
        "md5Section3": "matching-grid",
        "packingType": "grid_simple",
        "uvRelativeToGrid": 0,
        "bitmapPresent": 0,
        "Ni": 1377,
        "Nj": 657,
        "longitudeOfFirstGridPointInDegrees": 336.5,
        "longitudeOfLastGridPointInDegrees": 62.5,
        "latitudeOfFirstGridPointInDegrees": 29.5,
        "latitudeOfLastGridPointInDegrees": 70.5,
        "totalLength": 20,
    }
    return [
        {
            **metadata,
            **dict(zip(GRIB_FIELD_KEYS, field, strict=True)),
            "bottomLevel": round(field[4]) if field[:4] == _SOIL_TEMP else field[5],
            "offset": i * 20,
            "typeOfSecondFixedSurface": 255,
            "scaleFactorOfSecondFixedSurface": "MISSING",
            "scaledValueOfSecondFixedSurface": "MISSING",
            "scaledValueOfFirstFixedSurface": (
                _SOIL_TEMP_SELECTORS[field[4]] if field[:4] == _SOIL_TEMP else 0
            ),
        }
        for i, field in enumerate(sorted(_REQUIRED))
    ]


class TestIconForcing(TestCase):
    def test_download_honors_retry_after_and_reuses_cache(self) -> None:
        responses = iter(
            (
                httpx.Response(429, headers={"Retry-After": "2"}),
                httpx.Response(200, content=_FRAME),
            )
        )
        with (
            TemporaryDirectory() as tmp,
            httpx.Client(transport=httpx.MockTransport(lambda _: next(responses))) as client,
            patch("av_wrf.runner.icon_acquire.sleep") as delay,
        ):
            root = Path(tmp)
            cache = LocalFileStore(root)
            for _ in range(2):
                _download(
                    "forcing.grib2",
                    client=client,
                    mirrors=("https://example.invalid",),
                    cache=cache,
                    root=root,
                )
                self.assertEqual(cache.get_bytes("forcing.grib2"), _FRAME)
            delay.assert_called_once_with(2.0)

    def test_download_falls_back_from_malformed_mirrors(self) -> None:
        truncated = _FRAME[:8] + (24).to_bytes(8, "big") + _FRAME[-4:]
        bodies = iter((b"<html>Unavailable</html>", truncated, _FRAME * 2, _FRAME))
        transport = httpx.MockTransport(lambda _: httpx.Response(200, content=next(bodies)))
        with TemporaryDirectory() as tmp, httpx.Client(transport=transport) as client:
            root = Path(tmp)
            cache = LocalFileStore(root)
            _download(
                "forcing.grib2",
                client=client,
                mirrors=tuple(f"https://mirror-{i}.invalid" for i in range(4)),
                cache=cache,
                root=root,
            )
            self.assertEqual(cache.get_bytes("forcing.grib2"), _FRAME)

    def test_acquisition_rejects_prep_drift(self) -> None:
        product = bundle_for_recipe(CONFIG, ICON_EU).product.product_id
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = LocalFileStore(root / "store")
            _, lock = plan_product_cycle(
                CONFIG,
                store,
                product_id=product,
                publish_origin_utc="2026-11-12T00:00:00Z",
                attempt_id="prep-drift",
                mpi_ranks=1,
                build_manifest=BuildManifest("host-dev-unpinned", "w", "p", (), "g", "m"),
            )
            prep_cycle_namelists(
                CONFIG,
                store,
                product_id=product,
                cycle_partition=lock.cycle_partition,
                attempt_id=lock.attempt_id,
                work_dir=root,
            )
            plan = root / "forcing_subset.json"
            _, cycle, _, groups = _read_plan(plan, CONFIG)
            self.assertEqual(cycle, CYCLE)
            doc = json.loads(plan.read_text(encoding="utf-8"))
            self.assertEqual({str(hour): keys for hour, keys in groups.items()}, doc["file_keys"])
            for field in ("bbox_wsen_deg", "mirrors", "forecast_hours", "vtable_name"):
                with self.subTest(field=field):
                    plan.write_text(json.dumps({**doc, field: []}), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "Forcing plan differs"):
                        _read_plan(plan, CONFIG)

    def test_v1_inventory_reuses_static_fields_and_contains_all_levels(self) -> None:
        bundle = bundle_for_recipe(CONFIG, ICON_EU)
        ICON_EU.check_window(bundle.primary_source, bundle.domain, CYCLE, (0, 78))
        with self.assertRaisesRegex(ValueError, "hourly cadence ends at f078"):
            ICON_EU.check_window(bundle.primary_source, bundle.domain, CYCLE, (0, 79))
        with self.assertRaisesRegex(ValueError, "requires use_sh_qv"):
            source = replace(bundle.primary_source, use_sh_qv=False)
            ICON_EU.check_window(source, bundle.domain, CYCLE, (0, 22))
        by_hour = ICON_EU.file_keys(CYCLE, (0, 7))
        zero, seven = by_hour["0"], by_hour["7"]
        self.assertIsInstance(zero, tuple)
        self.assertIsInstance(seven, tuple)
        self.assertEqual(len(set(zero)), 475)
        self.assertEqual(len(set(seven)), 475)
        self.assertEqual(len(set(zero) & set(seven)), 79)
        for field in ("P/lvt1/150/lv1/74", "T_SO/lvt1/106/lv1/0.005", "W_SO/lvt1/106/lv1/7.29"):
            key = f"m/icon-eu/p/{field}/r/2026-11-11T18%3A00/s/PT007H00M.grib2"
            self.assertIn(key, seven)
        self.assertIn(" | 111 | 5 | | SOILT001 |", ICON_EU.vtable_text or "")
        self.assertIn(" | 112 | 729 | 2187 | SOILM999 |", ICON_EU.vtable_text or "")

    def test_rejects_wrong_wind_frame_valid_time_and_duplicate_levels(self) -> None:
        bundle = bundle_for_recipe(CONFIG, ICON_EU)
        rows = _messages()
        self.assertEqual(len(rows), 472)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "f007.grib2"
            path.write_bytes(b" " * (len(rows) * 20))
            with patch("av_wrf.runner.forcing_icon.inspect_grib", return_value=rows):
                ICON_EU.validate_file(path, CYCLE, bundle.primary_source, bundle.domain, 7)
                for key, value, error in (
                    ("uvRelativeToGrid", 1, "earth-relative winds"),
                    ("validityTime", 0, "Wrong ICON-EU source, cycle or valid time"),
                    ("packingType", "grid_ccsds", "Invalid GRIB2 framing or packing"),
                ):
                    with self.subTest(key=key), self.assertRaisesRegex(ValueError, error):
                        old = rows[0][key]
                        try:
                            rows[0][key] = value
                            ICON_EU.validate_file(
                                path, CYCLE, bundle.primary_source, bundle.domain, 7
                            )
                        finally:
                            rows[0][key] = old
                rows[1] = {**rows[0], "offset": 20}
                with self.assertRaisesRegex(ValueError, "Duplicate or unexpected ICON-EU field"):
                    ICON_EU.validate_file(path, CYCLE, bundle.primary_source, bundle.domain, 7)
