"""Exercise GFS forecast identity, coverage, framing, and manifest immutability."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from av_wrf.contracts import BuildManifest, RunLock, parse_iso_utc
from av_wrf.core import required_forcing_forecast_hours
from av_wrf.runner import LocalFileStore, check_product_config, load_bundle, plan_product_cycle
from av_wrf.runner.bundle import validate_bundle
from av_wrf.runner.mirrors import (
    _ATMOSPHERIC,
    _FIELD_KEYS,
    _SOIL,
    _SURFACE,
    verify_forcing_manifest,
    write_forcing_manifest,
)

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
        "gridType": "regular_ll",
        "typeOfLevel": "isobaricInhPa",
        "md5Section3": "matching-grid",
        "packingType": "grid_simple",
        "Ni": 1440,
        "Nj": 721,
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
        {**metadata, **dict(zip(_FIELD_KEYS, field, strict=True)), "offset": i * 20}
        for i, field in enumerate(fields)
    ]


class TestForcingManifest(TestCase):
    def test_candidate_without_recipe_is_reported_and_cannot_be_primary(self) -> None:
        config = ROOT / "config"
        product_id = next((config / "products").glob("*.yaml")).stem
        bundle = load_bundle(config, product_id)
        summary = check_product_config(config, product_id)
        self.assertEqual(summary.candidate_source, bundle.candidate_source.profile_id)
        self.assertFalse(summary.candidate_recipe_available)
        with self.assertRaisesRegex(ValueError, "No validated WPS forcing recipe"):
            validate_bundle(replace(bundle, primary_source=bundle.candidate_source))

    def test_rejects_wrong_cycle_grid_or_inventory(self) -> None:
        config = ROOT / "config"
        b = load_bundle(config, next((config / "products").glob("*.yaml")).stem)
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
            for path, rows in messages.items():
                path.write_bytes(b" " * (20 * len(rows)))
            args = grib, lock, b.primary_source
            with patch("av_wrf.runner.mirrors._inspect_grib", side_effect=messages.__getitem__):
                manifest = write_forcing_manifest(*args, files, domain=b.domain)
                self.assertEqual(verify_forcing_manifest(*args, domain=b.domain), manifest)
                first = files[min(files)]
                row = messages[first][0]
                for key, value, error in (
                    ("validityTime", -1, "Wrong GFS source, cycle or valid time"),
                    ("Ni", 2, "coverage disagrees"),
                    ("parameterNumber", 255, "Missing GFS surface/soil messages"),
                ):
                    old = row[key]
                    row[key] = value
                    with self.assertRaisesRegex(ValueError, error):
                        verify_forcing_manifest(*args, domain=b.domain)
                    row[key] = old
                first.write_bytes(first.read_bytes() + b"x")
                with self.assertRaisesRegex(ValueError, "SHA-256 mismatch"):
                    verify_forcing_manifest(*args, domain=b.domain)
