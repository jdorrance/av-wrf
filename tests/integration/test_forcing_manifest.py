"""Exercise GFS forecast identity, coverage, framing, and manifest immutability."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from av_wrf.contracts import BuildManifest, RunLock, parse_iso_utc
from av_wrf.core import required_forcing_forecast_hours
from av_wrf.runner import (
    LocalFileStore,
    check_product_config,
    plan_product_cycle,
    prep_cycle_namelists,
)
from av_wrf.runner.bundle import validate_bundle
from av_wrf.runner.forcing_gfs import _ATMOSPHERIC, _SOIL, _SURFACE, GFS
from av_wrf.runner.grib import GRIB_FIELD_KEYS
from av_wrf.runner.mirrors import (
    resolve_forcing_window,
    verify_forcing_manifest,
    write_forcing_manifest,
)
from tests.integration import bundle_for_recipe

ROOT = Path(__file__).resolve().parents[2]


def _messages(lock: RunLock, fhr: int) -> list[dict[str, object]]:
    cycle = parse_iso_utc(lock.forcing_cycle_utc)
    valid = cycle + timedelta(hours=fhr)
    metadata: dict[str, object] = {
        "edition": 2,
        "centre": 7,
        "generatingProcessIdentifier": 96 if fhr else 81,
        "productDefinitionTemplateNumber": 0,
        "dataDate": int(cycle.strftime("%Y%m%d")),
        "dataTime": int(cycle.strftime("%H%M")),
        "validityDate": int(valid.strftime("%Y%m%d")),
        "validityTime": int(valid.strftime("%H%M")),
        "iDirectionIncrementInDegrees": 0.25,
        "jDirectionIncrementInDegrees": 0.25,
        "iScansNegatively": 0,
        "jScansPositively": 0,
        "jPointsAreConsecutive": 0,
        "alternativeRowScanning": 0,
        "numberOfMissing": 0,
        "uvRelativeToGrid": 0,
        "gridType": "regular_ll",
        "typeOfLevel": "isobaricInhPa",
        "md5Section3": "matching-grid",
        "packingType": "grid_simple",
        "Ni": 1440,
        "Nj": 721,
        "numberOfDataPoints": 1440 * 721,
        "min": "1e-6",
        "max": 1.0,
        "average": 0.5,
        "longitudeOfFirstGridPointInDegrees": 0,
        "longitudeOfLastGridPointInDegrees": 359.75,
        "latitudeOfFirstGridPointInDegrees": 90,
        "latitudeOfLastGridPointInDegrees": -90,
        "totalLength": 20,
    }
    levels = (*range(10, 950, 25), 1000)
    fields = (
        *_SURFACE,
        *_SOIL,
        *((*param, 100, level, level) for param in _ATMOSPHERIC for level in levels),
    )
    return [
        {**metadata, **dict(zip(GRIB_FIELD_KEYS, field, strict=True)), "offset": i * 20}
        for i, field in enumerate(fields)
    ]


class TestForcingManifest(TestCase):
    def test_candidate_without_recipe_is_reported_and_cannot_be_primary(self) -> None:
        config = ROOT / "config"
        bundle = bundle_for_recipe(config, GFS)
        summary = check_product_config(config, bundle.product.product_id)
        self.assertEqual(summary.candidate_source, bundle.candidate_source.profile_id)
        self.assertFalse(summary.candidate_recipe_available)
        with self.assertRaisesRegex(ValueError, "No validated WPS forcing recipe"):
            validate_bundle(replace(bundle, primary_source=bundle.candidate_source))
        bad_cycle = replace(bundle.product, forcing_cycle_hour_utc=1)
        with self.assertRaisesRegex(ValueError, "No 01Z forcing cycle"):
            validate_bundle(replace(bundle, product=bad_cycle))

    def test_prep_lists_source_files_and_gfs_rejects_missing_hourly_tail(self) -> None:
        config = ROOT / "config"
        b = bundle_for_recipe(config, GFS)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            stage = LocalFileStore(root / "store")
            _, lock = plan_product_cycle(
                config,
                stage,
                product_id=b.product.product_id,
                attempt_id="test",
                publish_origin_utc=f"2026-11-12T{b.product.publish_origin_hour_utc:02d}:00:00Z",
                build_manifest=BuildManifest("host-dev-unpinned", "w", "p", (), "g", "m"),
                mpi_ranks=1,
                bundle=b,
            )
            work = root / "prep"
            prep_cycle_namelists(
                config,
                stage,
                product_id=b.product.product_id,
                cycle_partition=lock.cycle_partition,
                attempt_id=lock.attempt_id,
                work_dir=work,
                bundle=b,
            )
            subset = json.loads((work / "forcing_subset.json").read_bytes())
            hours = tuple(subset["forecast_hours"])
            cycle = parse_iso_utc(lock.forcing_cycle_utc)
            for hour in hours:
                self.assertEqual(
                    subset["file_keys"][str(hour)],
                    f"gfs.{cycle:%Y%m%d}/{cycle:%H}/atmos/gfs.t{cycle:%H}z.pgrb2.0p25.f{hour:03d}",
                )
            lead = 120 - hours[0] - lock.spinup_hours
            last = replace(lock, lead_hours=(lead,))
            self.assertEqual(
                resolve_forcing_window(last, b.primary_source, domain=b.domain)[2][-1], 120
            )
            with self.assertRaisesRegex(ValueError, "cadence ends at f120; cycle requires f121"):
                resolve_forcing_window(
                    replace(last, lead_hours=(lead + 1,)), b.primary_source, domain=b.domain
                )
            with self.assertRaisesRegex(ValueError, "cadence ends at f120"):
                long_output = replace(b.output, map_end_hour=123, hourly_through_hour=123)
                validate_bundle(replace(b, output=long_output))

    def test_rejects_wrong_cycle_grid_or_inventory(self) -> None:
        config = ROOT / "config"
        b = bundle_for_recipe(config, GFS)
        with TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            stage = LocalFileStore(root / "store")
            _, lock = plan_product_cycle(
                config,
                stage,
                product_id=b.product.product_id,
                attempt_id="test",
                publish_origin_utc=f"2026-11-12T{b.product.publish_origin_hour_utc:02d}:00:00Z",
                build_manifest=BuildManifest("host-dev-unpinned", "w", "p", (), "g", "m"),
                mpi_ranks=1,
                bundle=b,
            )
            lock = replace(lock, lead_hours=(1,))
            grib = root / "grib"
            grib.mkdir()
            hours = required_forcing_forecast_hours(lock, b.primary_source)
            files = {fhr: grib / str(fhr) for fhr in hours}
            messages = {path: _messages(lock, fhr) for fhr, path in files.items()}
            first = files[min(files)]
            soil = messages[first][len(_SURFACE)]
            soil.update(discipline=0, parameterNumber=0)
            for path, rows in messages.items():
                path.write_bytes(b" " * (20 * len(rows)))
            args = grib, lock, b.primary_source
            with patch(
                "av_wrf.runner.forcing_gfs.inspect_grib",
                side_effect=lambda path, _keys: messages[path],
            ):
                manifest = write_forcing_manifest(*args, files, domain=b.domain)
                self.assertEqual(verify_forcing_manifest(*args, domain=b.domain), manifest)
                row = messages[first][0]
                for key, value, error in (
                    ("validityTime", -1, "Wrong GFS source, cycle or valid time"),
                    ("Ni", 2, "coverage disagrees"),
                    ("iScansNegatively", 1, "coverage disagrees"),
                    ("jScansPositively", 1, "coverage disagrees"),
                    ("numberOfMissing", 1, "Missing values in required GFS field"),
                    ("packingType", "grid_jpeg", "Invalid GRIB2 framing or packing"),
                    ("numberOfDataPoints", 1, "Invalid GRIB2 data-point count"),
                    ("average", float("nan"), "Missing or invalid GRIB2 key average"),
                    ("parameterNumber", 255, "Missing GFS surface/soil messages"),
                ):
                    old = row[key]
                    row[key] = value
                    with self.assertRaisesRegex(ValueError, error):
                        verify_forcing_manifest(*args, domain=b.domain)
                    row[key] = old
                second = messages[first][1]
                messages[first][1] = {
                    **soil,
                    "discipline": 2,
                    "parameterNumber": 2,
                    "offset": second["offset"],
                }
                with self.assertRaisesRegex(ValueError, "Duplicate required GFS field"):
                    verify_forcing_manifest(*args, domain=b.domain)
                messages[first][1] = second
                first.write_bytes(first.read_bytes() + b"x")
                with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                    verify_forcing_manifest(*args, domain=b.domain)
