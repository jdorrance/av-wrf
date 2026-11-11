"""Integration tests: atomic store CAS, single-flight dispatch, and synthetic provenance."""

from __future__ import annotations

import json
import shutil
import struct
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from av_wrf.codecs import crc32c_u32  # noqa: E402
from av_wrf.contracts import BuildManifest, ReadyReceipt  # noqa: E402
from av_wrf.core import selected_lead_hours  # noqa: E402
from av_wrf.runner import (  # noqa: E402
    LocalFileStore,
    attempt_stage_prefix,
    json_bytes,
    load_build_manifest,
    load_bundle,
    plan_product_cycle,
    post_cycle_from_inputs,
    post_manifest_key,
    post_webhook_json,
    publish_cycle_receipt,
    reconcile_dispatch,
    sha256_hex,
)


def stage_fast_two_lead_cfg(src_cfg: Path, dst_cfg: Path) -> str:
    shutil.copytree(src_cfg, dst_cfg)
    prod_files = sorted((dst_cfg / "products").glob("*.yaml"))
    candidates = [load_bundle(dst_cfg, p.stem) for p in prod_files]
    best = min(
        candidates,
        key=lambda b: len(selected_lead_hours(b.output)) * b.domain.e_we * b.domain.e_sn,
    )
    (dst_cfg / "profiles" / "outputs" / "wrf-zarr-v3-2h.yaml").write_text(
        "profile_id: wrf-zarr-v3-2h\nschema_id: wrf-zarr-v3\nmap_end_hour: 2\n"
        "hourly_through_hour: 2\ntail_step_hours: 1\nshard_time_steps: 1\n"
        "inner_chunk_yx: [42, 42]\nzstd_level: 3\n",
        encoding="utf-8",
    )
    p_path = dst_cfg / "products" / f"{best.product.product_id}.yaml"
    lines = []
    for line in p_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("output_profile:"):
            lines.append("output_profile: wrf-zarr-v3-2h")
        elif line.startswith("min_horizon_hours:"):
            lines.append("min_horizon_hours: 1")
        elif line.startswith("max_cycle_age_hours:"):
            lines.append("max_cycle_age_hours: 1")
        else:
            lines.append(line)
    p_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return best.product.product_id


class TestLocalFileStoreAndDispatch(unittest.TestCase):
    def test_concurrent_cas_single_flight_dispatch_and_destination_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            with self.assertRaises(FileNotFoundError):
                load_build_manifest(tmp_p / "missing.json", "")
            with self.assertRaises(FileNotFoundError):
                load_build_manifest(tmp_p / "missing.json", "sha256:abc", allow_unpinned_dev=True)

            store = LocalFileStore(tmp_p / "store")
            seed_sha = store.compare_and_swap("upstream/m/latest.json", b'{"gen":0}', None)

            def attempt_cas(worker_id: int) -> bool:
                try:
                    store.compare_and_swap(
                        "upstream/m/latest.json", f'{{"gen":1,"w":{worker_id}}}'.encode(), seed_sha
                    )
                    return True
                except RuntimeError:
                    return False

            with ThreadPoolExecutor(max_workers=8) as pool:
                outcomes = list(pool.map(attempt_cas, range(8)))
            self.assertEqual((outcomes.count(True), outcomes.count(False)), (1, 7))

            receipt = ReadyReceipt(
                "wrf-zarr-v3",
                1,
                "wrf-synth-3km",
                "20261112/00",
                "2026-11-12T00:00:00Z",
                "att-01",
                "forecast.zarr",
                {"kind": "lambert-conformal"},
                38,
                (1, 2),
                ("2026-11-12T01:00:00Z", "2026-11-12T02:00:00Z"),
                ("2026-11-12T00:00:00Z", "2026-11-12T01:00:00Z"),
                ("2026-11-12T01:00:00Z", "2026-11-12T02:00:00Z"),
                ("terrain_m",),
                (("terrain_m/c/0/0", "abc"),),
                "cfg123",
                "noaa-gfs-0p25-v1",
                "sha256:test",
            )
            with self.assertRaises(ValueError):
                reconcile_dispatch(store, receipt, lambda r: post_webhook_json("", r))

            calls: list[str] = []

            def send_hook(rec: ReadyReceipt) -> int:
                calls.append(rec.attempt_id)
                return 202

            with ThreadPoolExecutor(max_workers=4) as pool:
                res = list(
                    pool.map(
                        lambda _: reconcile_dispatch(
                            store, receipt, send_hook, destination_id="https://a.example/hook"
                        ),
                        range(4),
                    )
                )
            self.assertEqual((res.count(True), res.count(False)), (1, 3))
            self.assertEqual(len(calls), 1)
            self.assertTrue(
                reconcile_dispatch(
                    store, receipt, send_hook, destination_id="https://b.example/hook"
                )
            )
            self.assertEqual(len(calls), 2)

    def test_synthetic_provenance_fence_and_corrupt_later_shard_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = LocalFileStore(tmp_path / "store")
            cfg = tmp_path / "cfg"
            prod_id = stage_fast_two_lead_cfg(ROOT / "config", cfg)
            b = load_bundle(cfg, prod_id)
            origin_iso = f"2026-11-12T{b.product.publish_origin_hour_utc:02d}:00:00Z"
            manifest = BuildManifest("sha256:test", "wrf-sha", "wps-sha", (), "gcc", "openmpi")
            _, lock = plan_product_cycle(
                cfg,
                store,
                product_id=prod_id,
                publish_origin_utc=origin_iso,
                attempt_id="att-synth",
                mpi_ranks=4,
                build_manifest=manifest,
            )
            post_cycle_from_inputs(
                cfg,
                store,
                product_id=prod_id,
                cycle_partition=lock.cycle_partition,
                attempt_id="att-synth",
                wrfout_dir=None,
                synthetic=True,
            )
            with self.assertRaises(ValueError):
                publish_cycle_receipt(
                    cfg,
                    store,
                    product_id=prod_id,
                    cycle_partition=lock.cycle_partition,
                    attempt_id="att-synth",
                )
            fix_rec = publish_cycle_receipt(
                cfg,
                store,
                product_id=prod_id,
                cycle_partition=lock.cycle_partition,
                attempt_id="att-synth",
                allow_synthetic=True,
                now_utc_iso="2026-11-12T00:30:00Z",
            )
            self.assertEqual(fix_rec.forcing_profile_id, "synthetic-fixture")
            self.assertIsNone(store.get_bytes(f"upstream/{b.product.model_id}/latest.json"))

            origin_iso2 = f"2026-11-13T{b.product.publish_origin_hour_utc:02d}:00:00Z"
            _, lock2 = plan_product_cycle(
                cfg,
                store,
                product_id=prod_id,
                publish_origin_utc=origin_iso2,
                attempt_id="att-zstd2",
                mpi_ranks=4,
                build_manifest=manifest,
            )
            post_cycle_from_inputs(
                cfg,
                store,
                product_id=prod_id,
                cycle_partition=lock2.cycle_partition,
                attempt_id="att-zstd2",
                wrfout_dir=None,
                synthetic=True,
            )
            stg_root = attempt_stage_prefix(prod_id, lock2.cycle_partition, "att-zstd2")
            later_p = tmp_path / "store" / stg_root / "forecast.zarr" / "temp_2m_k/c/1/0/0"
            raw_b = bytearray(later_p.read_bytes())
            raw_b[0:8] = b"\xff\xff\xff\xff\xff\xff\xff\xff"
            struct.pack_into("<I", raw_b, len(raw_b) - 4, crc32c_u32(bytes(raw_b[-68:-4])))
            later_p.write_bytes(bytes(raw_b))
            pm_p = (
                tmp_path / "store" / post_manifest_key(prod_id, lock2.cycle_partition, "att-zstd2")
            )
            pm_doc = json.loads(pm_p.read_text(encoding="utf-8"))
            pm_doc["shard_sha256s"] = [
                [k, sha256_hex(bytes(raw_b)) if k == "temp_2m_k/c/1/0/0" else s]
                for k, s in pm_doc["shard_sha256s"]
            ]
            pm_p.write_bytes(json_bytes(pm_doc))
            with self.assertRaises(ValueError):
                publish_cycle_receipt(
                    cfg,
                    store,
                    product_id=prod_id,
                    cycle_partition=lock2.cycle_partition,
                    attempt_id="att-zstd2",
                    allow_synthetic=True,
                )


if __name__ == "__main__":
    unittest.main()
