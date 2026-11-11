"""Contract tests verifying full wrf-zarr-v3 schema sync and 30-node Zarr v3 hierarchy."""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.cli import main as cli_main  # noqa: E402
from av_wrf.codecs import build_zarr_v3_store_metadata, zarr_shard_relpath  # noqa: E402
from av_wrf.contracts import (  # noqa: E402
    CAPE_PARCEL_DEFINITION,
    DEFAULT_INNER_SPATIAL_CHUNK,
    DEFAULT_SHARD_TIME_STEP,
    LOW_CLOUD_DEFINITION,
    MAX_EXPORTED_LEVELS,
    MOISTURE_SOURCE_OF_TRUTH,
    TEMPERATURE_DEFINITION,
    WRF_SPHERE_RADIUS_M,
    WRF_ZARR_V3_FIELDS,
    ZARR_FORMAT_VERSION,
    ZARR_SCHEMA_ID,
    ZARR_SCHEMA_STATUS,
    ZARR_SCHEMA_VERSION,
    parse_domain_profile,
    parse_output_profile,
    zarr_index_codecs,
    zarr_inner_codecs,
)
from av_wrf.core import selected_lead_hours  # noqa: E402
from av_wrf.runner import parse_declarative_yaml  # noqa: E402


class TestVendorZarrContract(unittest.TestCase):
    def test_python_zarr_schema_matches_vendored_json_contract_completely(self) -> None:
        schema_path = ROOT / "vendor" / "producer-contract" / "wrf-zarr-v3.schema.json"
        raw = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(raw["schemaId"], ZARR_SCHEMA_ID)
        self.assertEqual(raw["schemaVersion"], ZARR_SCHEMA_VERSION)
        self.assertEqual(raw["status"], ZARR_SCHEMA_STATUS)
        self.assertEqual(raw["zarrFormat"], ZARR_FORMAT_VERSION)
        self.assertEqual(raw["moistureSourceOfTruth"], MOISTURE_SOURCE_OF_TRUTH)
        self.assertEqual(raw["temperatureDefinition"], TEMPERATURE_DEFINITION)
        self.assertEqual(raw["capeParcelDefinition"], CAPE_PARCEL_DEFINITION)
        self.assertEqual(raw["lowCloudDefinition"], LOW_CLOUD_DEFINITION)
        self.assertEqual(raw["earthRadiusM"], WRF_SPHERE_RADIUS_M)
        self.assertEqual(raw["maxVerticalLevels"], MAX_EXPORTED_LEVELS)
        self.assertEqual(raw["shardTimeStep"], DEFAULT_SHARD_TIME_STEP)
        self.assertEqual(raw["innerSpatialChunk"], list(DEFAULT_INNER_SPATIAL_CHUNK))
        self.assertEqual(raw["innerCodecs"], zarr_inner_codecs())
        self.assertEqual(raw["indexCodecs"], zarr_index_codecs())

        vendored_fields = raw["fields"]
        self.assertEqual(len(vendored_fields), len(WRF_ZARR_V3_FIELDS))
        for idx in range(len(WRF_ZARR_V3_FIELDS)):
            expected = vendored_fields[idx]
            spec = WRF_ZARR_V3_FIELDS[idx]
            self.assertEqual(spec.name, expected["name"])
            self.assertEqual(list(spec.dims), expected["dims"])
            self.assertEqual(spec.units, expected["units"])
            self.assertEqual(spec.target, expected["target"])
            self.assertEqual(spec.time_support, expected["timeSupport"])
            self.assertEqual(spec.keepbits, expected["keepbits"])

    def test_complete_zarr_v3_hierarchy_and_dynamic_shard_keys(self) -> None:
        dom = parse_domain_profile(
            parse_declarative_yaml(
                ROOT / "config" / "profiles" / "domains" / "armenia-3km-v1.yaml"
            )
        )
        out = parse_output_profile(
            parse_declarative_yaml(
                ROOT / "config" / "profiles" / "outputs" / "wrf-zarr-v3-48h.yaml"
            )
        )
        docs = build_zarr_v3_store_metadata(dom, out, selected_lead_hours(out))
        # 1 root group + 7 coordinate arrays + 22 data arrays = 30 zarr.json documents
        self.assertEqual(len(docs), 30)
        temp_meta = docs["temp_k/zarr.json"]
        self.assertEqual(temp_meta["node_type"], "array")
        self.assertEqual(temp_meta["shape"], [40, 40, 148, 180])
        self.assertEqual(zarr_shard_relpath("temp_k", out, time_index=5), "temp_k/c/5/0/0/0")
        self.assertEqual(zarr_shard_relpath("terrain_m", out), "terrain_m/c/0/0")

    def test_cli_check_validates_armenia_product(self) -> None:
        rc = cli_main(
            ["check", "--product", "wrf-armenia-3km", "--config-dir", str(ROOT / "config")]
        )
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
