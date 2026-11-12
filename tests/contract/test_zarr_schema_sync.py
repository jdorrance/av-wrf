"""Contract tests verifying wrf-zarr-v3 sync, [64, 64] edge-padded shards, and ZEP-0002 guards."""

from __future__ import annotations

import dataclasses
import json
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
    decode_shard_f32_array,
    encode_2d_shard_bytes,
    encode_3d_shard_bytes,
    expected_shard_keys,
    outer_shard_yx,
    resolve_inner_chunk_yx,
    synthetic_thm_from_dry_theta,
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
    parse_output_profile,
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
    def test_python_zarr_schema_matches_vendored_layout(self) -> None:
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
        self.assertEqual(len(vendored_fields), 22)
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

    def test_zep0002_exact_64x64_partial_edge_tiles_and_tile_cap_guard(self) -> None:
        self.assertEqual(crc32c_u32(b"123456789"), 0xE3069283)

        prod_id = sorted((ROOT / "config" / "products").glob("*.yaml"))[0].stem
        base_out = load_bundle(ROOT / "config", prod_id).output
        self.assertEqual(resolve_inner_chunk_yx(149, 181, base_out), (64, 64))
        self.assertEqual(outer_shard_yx(149, 181, base_out), (192, 192))
        with self.assertRaisesRegex(ValueError, "cap"):
            resolve_inner_chunk_yx(1025, 1025, base_out)

        # Prime dimensions exercise padding on both axes with canonical 64x64 tiles.
        ny, nx = 149, 181
        slab = PubMass2D(Slab2DF32.from_values(ny, nx, [float(i) for i in range(ny * nx)]))
        shard2d = encode_2d_shard_bytes(slab, base_out)
        dec2d = decode_shard_f32_array(shard2d, nz=1, ny=ny, nx=nx, output=base_out)
        self.assertEqual(dec2d.ravel().tolist(), list(slab.buf))
        volume = PubMass3D(
            Slab3DF32.from_values(2, ny, nx, [float(i) * 0.5 for i in range(2 * ny * nx)])
        )
        shard3d = encode_3d_shard_bytes(volume, base_out)
        dec3d = decode_shard_f32_array(shard3d, nz=2, ny=ny, nx=nx, output=base_out)
        self.assertEqual(dec3d.ravel().tolist(), list(volume.buf))

        idx_len = 9 * 16
        body = shard3d[: -(idx_len + 4)]
        original_idx = shard3d[-(idx_len + 4) : -4]
        for offset, length in ((4, 8), (0xFFFFFFFFFFFFFFFF, 0xFFFFFFFFFFFFFFFF), (0, 0)):
            bad_idx = bytearray(original_idx)
            struct.pack_into("<QQ", bad_idx, 0, offset, length)
            forged = body + bad_idx + struct.pack("<I", crc32c_u32(bytes(bad_idx)))
            with self.subTest(offset=offset, length=length), self.assertRaises(ValueError):
                decode_shard_f32_array(forged, nz=2, ny=ny, nx=nx, output=base_out)
        bad_crc = shard3d[:-1] + bytes([shard3d[-1] ^ 1])
        with self.assertRaisesRegex(ValueError, "CRC-32C"):
            decode_shard_f32_array(bad_crc, nz=2, ny=ny, nx=nx, output=base_out)

    def test_complete_zarr_v3_hierarchy_exact_64x64_chunks_and_cli_check(self) -> None:
        cfg_dir = ROOT / "config"
        for p_path in sorted((cfg_dir / "products").glob("*.yaml")):
            b = load_bundle(cfg_dir, p_path.stem)
            leads = selected_lead_hours(b.output)
            docs = build_zarr_v3_store_metadata(b.domain, b.output, leads)
            self.assertEqual(len(docs), 30)
            group_attrs = docs["zarr.json"]["attributes"]
            assert isinstance(group_attrs, dict)
            self.assertEqual(group_attrs["cape_parcel_definition"], CAPE_PARCEL_DEFINITION)
            nj, ni = published_grid_shape(b.domain)
            temp_meta = docs["temp_k/zarr.json"]
            self.assertEqual(temp_meta["shape"], [len(leads), b.domain.exported_levels_k, nj, ni])
            codecs = temp_meta["codecs"]
            assert isinstance(codecs, list)
            shard_cfg = codecs[0]["configuration"]
            self.assertEqual(shard_cfg["chunk_shape"], [1, b.domain.exported_levels_k, 64, 64])
            self.assertEqual(zarr_shard_relpath("terrain_m", b.output), "terrain_m/c/0/0")
            self.assertIn("terrain_m/c/0/0", expected_shard_keys(b.output, len(leads)))
            for changed in (
                dataclasses.replace(b.output, schema_id="bad-schema"),
                dataclasses.replace(b.output, shard_time_steps=2),
                dataclasses.replace(b.output, inner_chunk_yx=(32, 64)),
                dataclasses.replace(b.output, zstd_level=4),
            ):
                with (
                    self.subTest(output=changed),
                    self.assertRaisesRegex(ValueError, ZARR_SCHEMA_ID),
                ):
                    build_zarr_v3_store_metadata(b.domain, changed, leads)
                with self.assertRaisesRegex(ValueError, ZARR_SCHEMA_ID):
                    parse_output_profile(dataclasses.asdict(changed))

        self.assertEqual(cli_main(["check", "--config-dir", str(ROOT / "config")]), 0)
        prod_id = sorted((ROOT / "config" / "products").glob("*.yaml"))[0].stem
        sci = load_bundle(ROOT / "config", prod_id).science
        t_dry = CompMass3D(Slab3DF32.from_values(1, 2, 2, [8.0] * 4))
        qv = CompMass3D(Slab3DF32.from_values(1, 2, 2, [0.012] * 4))
        p0 = CompMass3D(Slab3DF32.from_values(1, 2, 2, [0.0] * 4))
        pb = CompMass3D(Slab3DF32.from_values(1, 2, 2, [85_000.0] * 4))
        thm_val = synthetic_thm_from_dry_theta(8.0, 0.012)
        thm_ok = CompMass3D(Slab3DF32.from_values(1, 2, 2, [thm_val] * 4))
        verify_t_thm_consistency(t_dry, thm_ok, p0, pb, qv, sci)
        with self.assertRaises(ValueError):
            verify_t_thm_consistency(t_dry, t_dry, p0, pb, qv, sci)


if __name__ == "__main__":
    unittest.main()
