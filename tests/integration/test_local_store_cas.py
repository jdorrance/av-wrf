"""Integration tests: atomic store CAS, expiring webhook leases, and shard corruption checks."""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from av_wrf.contracts import BuildManifest, ReadyReceipt  # noqa: E402
from av_wrf.runner import (  # noqa: E402
    LocalFileStore,
    attempt_stage_prefix,
    dispatch_receipt_key,
    json_bytes,
    load_build_manifest,
    load_bundle,
    load_run_lock,
    plan_product_cycle,
    post_cycle_from_inputs,
    post_manifest_key,
    post_webhook_json,
    publish_cycle_receipt,
    reconcile_dispatch,
    sha256_hex,
)
from tests.integration import stage_sparse_config  # noqa: E402


class TestLocalFileStoreAndDispatch(unittest.TestCase):
    def test_atomic_writes_sync_file_before_install_and_sync_directories_after(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            key = "nested/object"
            events: list[str] = []
            sync, link, rename = os.fsync, os.link, os.replace

            def record_sync(fd: int) -> None:
                events.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
                sync(fd)

            def record_link(src: Path, dst: Path) -> None:
                events.append("link")
                link(src, dst)

            def record_rename(src: Path, dst: Path) -> None:
                events.append("rename")
                rename(src, dst)

            with patch("av_wrf.runner.store.os.fsync", side_effect=OSError("sync failed")):
                with self.assertRaisesRegex(OSError, "sync failed"):
                    store.put_if_absent(key, b"v1")
                self.assertIsNone(store.get_bytes(key))
            with (
                patch("av_wrf.runner.store.os.fsync", side_effect=record_sync),
                patch("av_wrf.runner.store.os.link", side_effect=record_link),
                patch("av_wrf.runner.store.os.replace", side_effect=record_rename),
            ):
                store.put_if_absent(key, b"v1")
                self.assertEqual(events, ["file", "link", "dir", "dir"])
                events.clear()
                store.compare_and_swap(key, b"v2", store.get_with_token(key)[1])
                self.assertEqual(events, ["file", "rename", "dir", "dir"])
            self.assertEqual(store.get_bytes(key), b"v2")

    def test_post_requires_planned_producer_code_and_loads_legacy_locks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = LocalFileStore(root)
            cfg = ROOT / "config"
            prod_id = next((cfg / "products").glob("*.yaml")).stem
            bundle = load_bundle(cfg, prod_id)
            hour = bundle.product.publish_origin_hour_utc
            build = BuildManifest("host-dev-unpinned", "wrf", "wps", (), "gcc", "mpi")

            def plan() -> str:
                key, _ = plan_product_cycle(
                    cfg,
                    store,
                    product_id=prod_id,
                    publish_origin_utc=f"2026-11-12T{hour:02d}:00:00Z",
                    attempt_id="att-code",
                    mpi_ranks=4,
                    build_manifest=build,
                    bundle=bundle,
                )
                return key

            key = plan()
            cycle = f"20261112/{hour:02d}"
            doc = json.loads((root / key).read_bytes())
            self.assertRegex(doc["producer_sha256"], r"^[0-9a-f]{64}$")
            with (
                patch("av_wrf.runner.post._producer_sha256", return_value="0" * 64),
                self.assertRaisesRegex(ValueError, "Producer code SHA-256 drifted"),
            ):
                post_cycle_from_inputs(
                    cfg,
                    store,
                    product_id=prod_id,
                    cycle_partition=cycle,
                    attempt_id="att-code",
                    wrfout_dir=root / "missing",
                    bundle=bundle,
                )
            del doc["producer_sha256"]
            (root / key).write_bytes(json_bytes(doc))
            self.assertEqual(plan(), key)
            legacy = load_run_lock(store, prod_id, cycle, "att-code", bundle=bundle)
            self.assertIsNone(legacy.producer_sha256)

    def test_publish_rejects_lock_fields_that_disagree_with_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = LocalFileStore(root)
            cfg = ROOT / "config"
            prod_id = next((cfg / "products").glob("*.yaml")).stem
            bundle = load_bundle(cfg, prod_id)
            origin = f"2026-11-12T{bundle.product.publish_origin_hour_utc:02d}:00:00Z"
            key, lock = plan_product_cycle(
                cfg,
                store,
                product_id=prod_id,
                publish_origin_utc=origin,
                attempt_id="att-lock",
                mpi_ranks=4,
                build_manifest=BuildManifest("host-dev-unpinned", "wrf", "wps", (), "gcc", "mpi"),
                bundle=bundle,
            )
            self.assertEqual(
                load_run_lock(store, prod_id, lock.cycle_partition, lock.attempt_id, bundle=bundle),
                lock,
            )
            for changed in (
                dataclasses.replace(lock, model_id="other-model"),
                dataclasses.replace(lock, lead_hours=()),
                dataclasses.replace(lock, publish_origin_utc=origin.replace(":00:00Z", ":30:00Z")),
                dataclasses.replace(lock, mpi_ranks=-1),
            ):
                (root / key).write_bytes(json_bytes(changed.to_doc()))
                with self.assertRaisesRegex(ValueError, "RunLock (disagrees|mpi_ranks)"):
                    publish_cycle_receipt(
                        cfg,
                        store,
                        product_id=prod_id,
                        cycle_partition=lock.cycle_partition,
                        attempt_id=lock.attempt_id,
                        bundle=bundle,
                    )

    def test_concurrent_cas_single_flight_and_expired_lease_takeover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            with self.assertRaises(FileNotFoundError):
                load_build_manifest(tmp_p / "missing.json", "")
            with self.assertRaises(FileNotFoundError):
                load_build_manifest(tmp_p / "missing.json", "sha256:abc", allow_unpinned_dev=True)

            store = LocalFileStore(tmp_p / "store")
            seed_tok = store.compare_and_swap("upstream/m/latest.json", b'{"gen":0}', None)

            def attempt_cas(worker_id: int) -> bool:
                try:
                    store.compare_and_swap(
                        "upstream/m/latest.json", f'{{"gen":1,"w":{worker_id}}}'.encode(), seed_tok
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

            # Simulate crashed worker leaving stale in_flight claim on destination c:
            c_url = "https://c.example/hook"
            c_key = dispatch_receipt_key(receipt, c_url)
            store.compare_and_swap(
                c_key,
                json_bytes(
                    {
                        "state": "in_flight",
                        "claim_id": "dead-worker",
                        "claimed_at_unix_s": 1000.0,
                        "lease_seconds": 60.0,
                    }
                ),
                None,
            )
            # Active lease at t=1030 blocks duplicate delivery
            self.assertFalse(
                reconcile_dispatch(
                    store, receipt, send_hook, destination_id=c_url, now_unix_s=1030.0
                )
            )
            # Expired lease at t=1065 allows clean CAS takeover and delivery
            self.assertTrue(
                reconcile_dispatch(
                    store, receipt, send_hook, destination_id=c_url, now_unix_s=1065.0
                )
            )

    def test_synthetic_provenance_fence_and_corrupt_pressure_shard_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            store = LocalFileStore(tmp_path / "store")
            cfg = tmp_path / "cfg"
            prod_id = stage_sparse_config(ROOT / "config", cfg)
            b = load_bundle(cfg, prod_id)
            origin_iso = f"2026-11-12T{b.product.publish_origin_hour_utc:02d}:00:00Z"
            manifest = BuildManifest("host-dev-unpinned", "wrf-sha", "wps-sha", (), "gcc", "mpi")
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
                attempt_id="att-crc",
                mpi_ranks=4,
                build_manifest=manifest,
            )
            post_cycle_from_inputs(
                cfg,
                store,
                product_id=prod_id,
                cycle_partition=lock2.cycle_partition,
                attempt_id="att-crc",
                wrfout_dir=None,
                synthetic=True,
            )
            stg_root = attempt_stage_prefix(prod_id, lock2.cycle_partition, "att-crc")
            later_p = tmp_path / "store" / stg_root / "forecast.zarr" / "temp_2m_k/c/1/0/0"
            raw_b = bytearray(later_p.read_bytes())
            raw_b[-1] ^= 0xFF
            later_p.write_bytes(bytes(raw_b))
            pm_p = tmp_path / "store" / post_manifest_key(prod_id, lock2.cycle_partition, "att-crc")
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
                    attempt_id="att-crc",
                    allow_synthetic=True,
                )


if __name__ == "__main__":
    unittest.main()
