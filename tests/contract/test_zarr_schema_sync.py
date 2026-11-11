"""Contract tests verifying wrf-zarr-v3 sync, 30-node Zarr v3 metadata, and ZEP-0002 shards."""

from __future__ import annotations

import dataclasses
import json
import math
import struct
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for p in (ROOT, ROOT / "src"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from av_wrf.cli import main as cli_main  # noqa: E402
from av_wrf.codecs import (  # noqa: E402
    build_zarr_v3_store_metadata,
    crc32c_u32,
    decode_shard_f32,
    encode_2d_shard_bytes,
    encode_3d_shard_bytes,
    expected_shard_keys,
    synthetic_thm_from_dry_theta,
    verify_shard_index,
    zarr_shard_relpath,
)
from av_wrf.contracts import (  # noqa: E402
    CAPE_PARCEL_DEFINITION,
    DEFAULT_INNER_SPATIAL_CHUNK,
    DEFAULT_SHARD_TIME_STEP,
    LOW_CLOUD_DEFINITION,
    MAX_EXPORTED_LEVELS,
    MOISTURE_SOURCE_OF_TRUTH,
    TEMPERATURE_DEFINITION,
    WRF_SPHERE_RADIUS_M,
    WRF_ZARR_V3_COORDS,
    WRF_ZARR_V3_FIELDS,
    ZARR_FORMAT_VERSION,
    ZARR_SCHEMA_ID,
    ZARR_SCHEMA_STATUS,
    ZARR_SCHEMA_VERSION,
    CompMass3D,
    PubMass2D,
    PubMass3D,
    Slab2DF32,
    Slab3DF32,
    zarr_index_codecs,
    zarr_inner_codecs,
)
from av_wrf.core import (  # noqa: E402
    published_grid_shape,
    selected_lead_hours,
    verify_t_thm_consistency,
)
from av_wrf.runner import load_bundle  # noqa: E402


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
        self.assertEqual(len(WRF_ZARR_V3_COORDS), 7)

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

    def test_zep0002_multitile_partial_shard_and_index_verification(self) -> None:
        self.assertEqual(crc32c_u32(b"123456789"), 0xE3069283)

        prod_id = sorted((ROOT / "config" / "products").glob("*.yaml"))[0].stem
        out = dataclasses.replace(
            load_bundle(ROOT / "config", prod_id).output,
            inner_chunk_yx=(5, 7),
            shard_time_steps=3,
        )
        f0 = PubMass2D(Slab2DF32.from_values(10, 14, [float(i) for i in range(140)]))
        f1 = PubMass2D(Slab2DF32.from_values(10, 14, [float(i) + 500.0 for i in range(140)]))
        shard2d = encode_2d_shard_bytes([f0, f1], out, shard_time_steps=3)
        dec2d = decode_shard_f32(
            shard2d, shard_time_steps=3, n_frames=2, nz=1, ny=10, nx=14, output=out
        )
        self.assertEqual(list(dec2d[:140]), list(f0.buf))
        self.assertEqual(list(dec2d[140:280]), list(f1.buf))
        self.assertTrue(all(math.isnan(v) for v in dec2d[280:]))
        # Claiming 3 populated frames when slot 2 holds sentinels must fail index verification
        with self.assertRaises(ValueError):
            verify_shard_index(shard2d, shard_time_steps=3, n_frames=3, ny=10, nx=14, output=out)

        v0 = PubMass3D(Slab3DF32.from_values(2, 10, 14, [float(i) * 0.5 for i in range(280)]))
        v1 = PubMass3D(Slab3DF32.from_values(2, 10, 14, [float(i) * 1.5 for i in range(280)]))
        shard3d = encode_3d_shard_bytes([v0, v1], out, shard_time_steps=2)
        dec3d = decode_shard_f32(shard3d, shard_time_steps=2, nz=2, ny=10, nx=14, output=out)
        self.assertEqual(list(dec3d), [*v0.buf, *v1.buf])

        # Re-CRC'd bad offset in tile 0 must still fail verify_shard_index bounds check
        idx_len = 8 * 16
        body = shard3d[: -(idx_len + 4)]
        bad_idx = bytearray(shard3d[-(idx_len + 4) : -4])
        struct.pack_into("<QQ", bad_idx, 0, 4, 8)
        forged = body + bytes(bad_idx) + struct.pack("<I", crc32c_u32(bytes(bad_idx)))
        with self.assertRaises(ValueError):
            verify_shard_index(forged, shard_time_steps=2, n_frames=2, ny=10, nx=14, output=out)

    def test_complete_zarr_v3_hierarchy_for_all_configured_products(self) -> None:
        cfg_dir = ROOT / "config"
        for p_path in sorted((cfg_dir / "products").glob("*.yaml")):
            b = load_bundle(cfg_dir, p_path.stem)
            leads = selected_lead_hours(b.output)
            docs = build_zarr_v3_store_metadata(b.domain, b.output, leads)
            self.assertEqual(len(docs), 30)
            nj, ni = published_grid_shape(b.domain)
            temp_meta = docs["temp_k/zarr.json"]
            self.assertEqual(temp_meta["shape"], [len(leads), b.domain.exported_levels_k, nj, ni])
            self.assertEqual(zarr_shard_relpath("terrain_m", b.output), "terrain_m/c/0/0")
            self.assertIn("terrain_m/c/0/0", expected_shard_keys(b.output, len(leads)))

    def test_cli_check_and_t_thm_guard(self) -> None:
        self.assertEqual(cli_main(["check", "--config-dir", str(ROOT / "config")]), 0)
        prod_id = sorted((ROOT / "config" / "products").glob("*.yaml"))[0].stem
        sci = load_bundle(ROOT / "config", prod_id).science
        t_dry = CompMass3D(Slab3DF32.from_values(1, 2, 2, [8.0] * 4))
        qv = CompMass3D(Slab3DF32.from_values(1, 2, 2, [0.012] * 4))
        p0 = CompMass3D(Slab3DF32.from_values(1, 2, 2, [0.0] * 4))
        pb = CompMass3D(Slab3DF32.from_values(1, 2, 2, [85_000.0] * 4))
        thm_val = synthetic_thm_from_dry_theta(8.0, 0.012, sci.base_temp_k)
        thm_ok = CompMass3D(Slab3DF32.from_values(1, 2, 2, [thm_val] * 4))
        verify_t_thm_consistency(t_dry, thm_ok, p0, pb, qv, sci)
        with self.assertRaises(ValueError):
            verify_t_thm_consistency(t_dry, t_dry, p0, pb, qv, sci)


if __name__ == "__main__":
    unittest.main()
