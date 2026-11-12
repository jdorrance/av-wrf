"""Integration tests: concurrent publish race, multi-store stage retention, and recovery."""

from __future__ import annotations

import dataclasses
import json
import sys
import tempfile
import threading
import unittest
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from av_wrf.codecs import (  # noqa: E402
    build_synthetic_wrf_baseline,
    build_synthetic_wrf_step,
    decode_shard_f32,
)
from av_wrf.contracts import (  # noqa: E402
    BuildManifest,
    CompMass2D,
    DiagnosticsReceipt,
    RawWrfStepInput,
    Slab2DF32,
    SolverReceipt,
    parse_iso_utc,
)
from av_wrf.core import resolve_cycle_window  # noqa: E402
from av_wrf.runner import (  # noqa: E402
    LoadedBundle,
    LocalFileStore,
    attempt_stage_prefix,
    json_bytes,
    load_bundle,
    plan_product_cycle,
    post_cycle_store,
    post_manifest_key,
    publish_cycle_receipt,
    put_idempotent,
    sha256_hex,
    solver_receipt_key,
    verify_solver_receipt,
)
from tests.integration import stage_sparse_config  # noqa: E402


def _seed_solver_receipt(
    store: LocalFileStore,
    b: LoadedBundle,
    cycle_partition: str,
    attempt_id: str,
    oci: str,
    output_shas: tuple[tuple[str, str], ...] = (("wrfout_d01_00", "w0"),),
) -> SolverReceipt:
    srec = SolverReceipt(
        b.product.product_id,
        cycle_partition,
        attempt_id,
        b.config_sha256,
        oci,
        "a" * 64,
        (("gfs.f000", "f0"),),
        output_shas,
    )
    put_idempotent(
        store,
        solver_receipt_key(b.product.product_id, cycle_partition, attempt_id),
        json_bytes(srec.to_doc()),
    )
    return srec


def _stage_attempt_with_t2(
    cfg: Path,
    store: LocalFileStore,
    b: LoadedBundle,
    origin_iso: str,
    attempt_id: str,
    t2_val: float,
) -> str:
    manifest = BuildManifest("sha256:" + "b" * 64, "wrf-sha", "wps-sha", (), "gcc", "openmpi")
    win = resolve_cycle_window(b.product, b.output, origin_iso)
    plan_product_cycle(
        cfg,
        store,
        product_id=b.product.product_id,
        publish_origin_utc=origin_iso,
        attempt_id=attempt_id,
        mpi_ranks=4,
        build_manifest=manifest,
        geog_bundle_sha256="a" * 64,
        bundle=b,
    )
    _seed_solver_receipt(store, b, win.cycle_partition, attempt_id, manifest.oci_digest)
    base_unix = int(parse_iso_utc(origin_iso).timestamp())
    ny, nx = b.domain.e_sn - 1, b.domain.e_we - 1

    def stream_leads() -> Iterator[RawWrfStepInput]:
        for h, vt in zip(win.lead_hours, win.valid_times_utc, strict=True):
            step = build_synthetic_wrf_step(b.domain, vt, base_unix + h * 3600, h)
            yield dataclasses.replace(
                step, t2_k=CompMass2D(Slab2DF32.from_values(ny, nx, [t2_val] * (ny * nx)))
            )

    post_cycle_store(
        cfg,
        store,
        product_id=b.product.product_id,
        cycle_partition=win.cycle_partition,
        attempt_id=attempt_id,
        f000_step=build_synthetic_wrf_baseline(b.domain, origin_iso, base_unix),
        lead_steps=stream_leads(),
        bundle=b,
    )
    return win.cycle_partition


class TestPublishRecoveryAndConcurrency(unittest.TestCase):
    def test_solver_receipt_hashes_consumed_frames_and_checks_full_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store, cfg = LocalFileStore(root / "store"), root / "cfg"
            prod_id = stage_sparse_config(ROOT / "config", cfg)
            b = load_bundle(cfg, prod_id)
            origin = f"2026-11-12T{b.product.publish_origin_hour_utc:02d}:00:00Z"
            oci = "sha256:" + "b" * 64
            _, lock = plan_product_cycle(
                cfg,
                store,
                product_id=prod_id,
                publish_origin_utc=origin,
                attempt_id="att-hash",
                mpi_ranks=4,
                build_manifest=BuildManifest(oci, "w", "p", (), "gcc", "mpi"),
                geog_bundle_sha256="a" * 64,
                bundle=b,
            )
            f0, skipped, f3 = (root / f"wrfout_d01_{hr:02d}" for hr in (0, 2, 3))
            for file in (f0, skipped, f3):
                file.write_bytes(b"original")
            shas = tuple((p.name, sha256_hex(b"original")) for p in (f0, skipped, f3))
            rec = _seed_solver_receipt(store, b, lock.cycle_partition, lock.attempt_id, oci, shas)
            skipped.write_bytes(b"changed")
            self.assertEqual(
                verify_solver_receipt(store, lock, root, selected_wrfouts={f0, f3}), rec
            )
            with self.assertRaisesRegex(ValueError, f"{skipped.name}.*SHA-256"):
                verify_solver_receipt(store, lock, root)
            f3.write_bytes(b"changed")
            with self.assertRaisesRegex(ValueError, f"{f3.name}.*SHA-256"):
                verify_solver_receipt(store, lock, root, selected_wrfouts={f0, f3})
            skipped.unlink()
            with self.assertRaisesRegex(ValueError, "inventory"):
                verify_solver_receipt(store, lock, root, selected_wrfouts={f0, f3})

    def test_concurrent_two_attempt_publish_race_preserves_atomic_winner(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = Path(tmp) / "store"
            store = LocalFileStore(store_dir)
            cfg = Path(tmp) / "cfg"
            prod_id = stage_sparse_config(ROOT / "config", cfg)
            b = load_bundle(cfg, prod_id)
            origin_iso = f"2026-11-12T{b.product.publish_origin_hour_utc:02d}:00:00Z"
            part = _stage_attempt_with_t2(cfg, store, b, origin_iso, "att-A", 285.0)
            _stage_attempt_with_t2(cfg, store, b, origin_iso, "att-B", 308.0)

            barrier = threading.Barrier(2)

            def race_publish(att_id: str) -> str:
                barrier.wait()
                try:
                    rec = publish_cycle_receipt(
                        cfg,
                        store,
                        product_id=prod_id,
                        cycle_partition=part,
                        attempt_id=att_id,
                        bundle=b,
                        now_utc_iso="2026-11-12T00:30:00Z",
                    )
                    return f"won:{rec.attempt_id}"
                except FileExistsError:
                    return "lost"

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = sorted(pool.map(race_publish, ("att-A", "att-B")))
            self.assertEqual(len([x for x in outcomes if x.startswith("won:")]), 1)
            self.assertEqual(outcomes.count("lost"), 1)
            winner = next(x.split(":")[1] for x in outcomes if x.startswith("won:"))
            exp_t2 = 285.0 if winner == "att-A" else 308.0

            ready_raw = store.get_bytes(f"upstream/{b.product.model_id}/{part}/_ready.json")
            assert ready_raw is not None
            ready_doc = json.loads(ready_raw.decode("utf-8"))
            self.assertEqual(ready_doc["attempt_id"], winner)
            pub_t2 = store.get_bytes(
                f"upstream/{b.product.model_id}/{part}/forecast.zarr/temp_2m_k/c/0/0/0"
            )
            assert pub_t2 is not None
            nj = b.domain.e_sn - 1 - 2 * b.domain.bdy_trim_cells
            ni = b.domain.e_we - 1 - 2 * b.domain.bdy_trim_cells
            dec = decode_shard_f32(pub_t2, shard_time_steps=1, nz=1, ny=nj, nx=ni, output=b.output)
            self.assertAlmostEqual(dec[0], exp_t2, delta=0.05)

    def test_stage_retention_multi_destination_replay_and_prune_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            stg_dir = Path(tmp) / "stage"
            stg = LocalFileStore(stg_dir)
            dst1 = LocalFileStore(Path(tmp) / "dst1")
            dst2 = LocalFileStore(Path(tmp) / "dst2")
            cfg = Path(tmp) / "cfg"
            prod_id = stage_sparse_config(ROOT / "config", cfg)
            b = load_bundle(cfg, prod_id)
            origin_iso = f"2026-11-12T{b.product.publish_origin_hour_utc:02d}:00:00Z"
            part = _stage_attempt_with_t2(cfg, stg, b, origin_iso, "att-replay", 296.0)

            # Promote to dst1 without pruning stage -> stage remains intact for dst2!
            rec1 = publish_cycle_receipt(
                cfg,
                dst1,
                product_id=prod_id,
                cycle_partition=part,
                attempt_id="att-replay",
                bundle=b,
                stage_store=stg,
                prune_stage=False,
                now_utc_iso="2026-11-12T00:30:00Z",
            )
            stage_path = stg_dir / attempt_stage_prefix(prod_id, part, "att-replay")
            self.assertTrue(stage_path.exists())
            rec2 = publish_cycle_receipt(
                cfg,
                dst2,
                product_id=prod_id,
                cycle_partition=part,
                attempt_id="att-replay",
                bundle=b,
                stage_store=stg,
                prune_stage=True,
                now_utc_iso="2026-11-12T00:30:00Z",
            )
            self.assertEqual(rec1, rec2)
            self.assertFalse(stage_path.exists())
            diag_raw = dst2.get_bytes(f"upstream/{b.product.model_id}/{part}/_diagnostics.json")
            assert diag_raw is not None
            diag = DiagnosticsReceipt.from_doc(json.loads(diag_raw.decode("utf-8")))
            self.assertGreaterEqual(diag.max_cfl, 0.0)

    def test_stale_backfill_and_tampered_coord_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = Path(tmp) / "store"
            store = LocalFileStore(store_dir)
            cfg = Path(tmp) / "cfg"
            prod_id = stage_sparse_config(ROOT / "config", cfg)
            b = load_bundle(cfg, prod_id)
            stale_iso = f"2020-01-01T{b.product.publish_origin_hour_utc:02d}:00:00Z"
            part = _stage_attempt_with_t2(cfg, store, b, stale_iso, "att-2020", 290.0)
            stg_2020 = attempt_stage_prefix(prod_id, part, "att-2020")
            coord_p = store_dir / stg_2020 / "forecast.zarr" / "time/c/0"
            good_coord = coord_p.read_bytes()
            bad_coord = b"\x00" * len(good_coord)
            coord_p.write_bytes(bad_coord)
            pm_p = store_dir / post_manifest_key(prod_id, part, "att-2020")
            pm_doc = json.loads(pm_p.read_text(encoding="utf-8"))
            good_pm = dict(pm_doc)
            pm_doc["shard_sha256s"] = [
                [k, sha256_hex(bad_coord) if k == "time/c/0" else s]
                for k, s in pm_doc["shard_sha256s"]
            ]
            pm_p.write_bytes(json_bytes(pm_doc))
            with self.assertRaises(ValueError):
                publish_cycle_receipt(
                    cfg,
                    store,
                    product_id=prod_id,
                    cycle_partition=part,
                    attempt_id="att-2020",
                    bundle=b,
                    now_utc_iso="2026-11-12T00:30:00Z",
                )
            coord_p.write_bytes(good_coord)
            pm_p.write_bytes(json_bytes(good_pm))
            publish_cycle_receipt(
                cfg,
                store,
                product_id=prod_id,
                cycle_partition=part,
                attempt_id="att-2020",
                bundle=b,
                now_utc_iso="2026-11-12T00:30:00Z",
            )
            self.assertIsNone(store.get_bytes(f"upstream/{b.product.model_id}/latest.json"))

    def test_rejects_corrupt_zstd_payload_with_valid_index_and_manifest_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "cfg"
            prod_id = stage_sparse_config(ROOT / "config", cfg)
            b = load_bundle(cfg, prod_id)
            stage, dst = LocalFileStore(root / "stage"), LocalFileStore(root / "dst")
            attempt_id = "att-corrupt"
            origin = f"2026-11-12T{b.product.publish_origin_hour_utc:02d}:00:00Z"
            part = _stage_attempt_with_t2(cfg, stage, b, origin, attempt_id, 290.0)
            stage_prefix = attempt_stage_prefix(prod_id, part, attempt_id)
            shard = "temp_2m_k/c/0/0/0"
            shard_file = root / "stage" / stage_prefix / "forecast.zarr" / shard
            valid_shard = shard_file.read_bytes()
            corrupt_shard = bytes([valid_shard[0] ^ 1]) + valid_shard[1:]
            shard_file.write_bytes(corrupt_shard)
            manifest_file = root / "stage" / post_manifest_key(prod_id, part, attempt_id)
            valid_manifest = manifest_file.read_bytes()
            corrupt_manifest = json.loads(valid_manifest)
            corrupt_manifest["shard_sha256s"] = [
                [key, sha256_hex(corrupt_shard) if key == shard else sha]
                for key, sha in corrupt_manifest["shard_sha256s"]
            ]
            manifest_file.write_bytes(json_bytes(corrupt_manifest))

            def publish() -> None:
                publish_cycle_receipt(
                    cfg,
                    dst,
                    product_id=prod_id,
                    cycle_partition=part,
                    attempt_id=attempt_id,
                    bundle=b,
                    stage_store=stage,
                )

            with self.assertRaisesRegex(ValueError, "Corrupt Zstd payload"):
                publish()
            ready_key = f"upstream/{b.product.model_id}/{part}/_ready.json"
            self.assertIsNone(dst.get_bytes(ready_key))
            shard_file.write_bytes(valid_shard)
            manifest_file.write_bytes(valid_manifest)
            publish()
            self.assertIsNotNone(dst.get_bytes(ready_key))


if __name__ == "__main__":
    unittest.main()
