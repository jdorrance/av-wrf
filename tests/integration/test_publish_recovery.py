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
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from av_wrf.codecs import (  # noqa: E402
    build_synthetic_wrf_baseline,
    build_synthetic_wrf_step,
    decode_shard_f32_array,
)
from av_wrf.contracts import (  # noqa: E402
    BuildManifest,
    CompMass2D,
    DiagnosticsReceipt,
    RawWrfStepInput,
    ReadyReceipt,
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
    load_ready_receipt,
    plan_product_cycle,
    post_manifest_key,
    publish_cycle_receipt,
    put_idempotent,
    sha256_hex,
    solver_receipt_key,
    verify_solver_receipt,
)
from av_wrf.runner.post import _stage_cycle  # noqa: E402
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
    _, lock = plan_product_cycle(
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

    _stage_cycle(
        store,
        b,
        lock,
        win,
        build_synthetic_wrf_baseline(b.domain, origin_iso, base_unix),
        stream_leads(),
        "solver",
    )
    return win.cycle_partition


class TestPublishRecoveryAndConcurrency(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.cfg = self.root / "cfg"
        self.prod_id = stage_sparse_config(ROOT / "config", self.cfg)
        self.bundle = load_bundle(self.cfg, self.prod_id)

    def test_solver_receipt_hashes_consumed_frames_and_checks_full_inventory(self) -> None:
        prod_id, b, root = self.prod_id, self.bundle, self.root
        store, cfg = LocalFileStore(root / "store"), self.cfg
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
        self.assertEqual(verify_solver_receipt(store, lock, root, selected_wrfouts={f0, f3}), rec)
        with self.assertRaisesRegex(ValueError, f"{skipped.name}.*SHA-256"):
            verify_solver_receipt(store, lock, root)
        f3.write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, f"{f3.name}.*SHA-256"):
            verify_solver_receipt(store, lock, root, selected_wrfouts={f0, f3})
        skipped.unlink()
        with self.assertRaisesRegex(ValueError, "inventory"):
            verify_solver_receipt(store, lock, root, selected_wrfouts={f0, f3})

    def test_concurrent_two_attempt_publish_race_preserves_atomic_winner(self) -> None:
        prod_id, b = self.prod_id, self.bundle
        cfg = self.cfg
        store_dir = self.root / "store"
        store = LocalFileStore(store_dir)
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
        ready = load_ready_receipt(store, b.product.model_id, part)
        self.assertEqual(ready.attempt_id, winner)
        shard = "forecast.zarr/temp_2m_k/c/0/0/0"
        pub_key = f"upstream/{b.product.model_id}/{part}/{shard}"
        pub_t2 = store.get_bytes(pub_key)
        assert pub_t2 is not None
        nj = b.domain.e_sn - 1 - 2 * b.domain.bdy_trim_cells
        ni = b.domain.e_we - 1 - 2 * b.domain.bdy_trim_cells
        dec = decode_shard_f32_array(pub_t2, nz=1, ny=nj, nx=ni, output=b.output)
        self.assertAlmostEqual(dec.flat[0], exp_t2, delta=0.05)
        staged = store_dir / attempt_stage_prefix(prod_id, part, winner) / shard
        staged.write_bytes(b"damaged stage")
        self.assertEqual(store.get_bytes(pub_key), pub_t2)

    def test_stage_retention_multi_destination_replay_and_prune_flag(self) -> None:
        prod_id, b = self.prod_id, self.bundle
        cfg = self.cfg
        stg_dir = self.root / "stage"
        stg = LocalFileStore(stg_dir)
        dst1 = LocalFileStore(self.root / "dst1")
        dst2 = LocalFileStore(self.root / "dst2")
        origin_iso = f"2026-11-12T{b.product.publish_origin_hour_utc:02d}:00:00Z"
        part = _stage_attempt_with_t2(cfg, stg, b, origin_iso, "att-replay", 296.0)

        def promote(dst: LocalFileStore, *, prune: bool) -> ReadyReceipt:
            return publish_cycle_receipt(
                cfg,
                dst,
                product_id=prod_id,
                cycle_partition=part,
                attempt_id="att-replay",
                bundle=b,
                stage_store=stg,
                prune_stage=prune,
            )

        rec1 = promote(dst1, prune=False)
        stage_path = stg_dir / attempt_stage_prefix(prod_id, part, "att-replay")
        self.assertTrue(stage_path.exists())
        rec2 = promote(dst2, prune=True)
        self.assertEqual(rec1, rec2)
        self.assertFalse(stage_path.exists())
        self.assertEqual(promote(dst2, prune=True), rec2)
        with self.assertRaisesRegex(FileNotFoundError, "Missing post_manifest.json"):
            promote(LocalFileStore(self.root / "dst3"), prune=False)
        diag_raw = dst2.get_bytes(f"upstream/{b.product.model_id}/{part}/_diagnostics.json")
        assert diag_raw is not None
        self.assertGreaterEqual(DiagnosticsReceipt.from_doc(json.loads(diag_raw)).max_cfl, 0.0)
        wrong_part = f"20261113/{part.split('/')[1]}"
        dst2.put_if_absent(
            f"upstream/{rec2.model_id}/{wrong_part}/_ready.json", json_bytes(rec2.to_doc())
        )
        with self.assertRaisesRegex(ValueError, "receipt identity"):
            load_ready_receipt(dst2, rec2.model_id, wrong_part)

    def test_stale_backfill_and_tampered_coord_rejection(self) -> None:
        prod_id, b = self.prod_id, self.bundle
        cfg = self.cfg
        store_dir = self.root / "store"
        store = LocalFileStore(store_dir)
        stale_iso = f"2020-01-01T{b.product.publish_origin_hour_utc:02d}:00:00Z"
        stage = store
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
            [k, sha256_hex(bad_coord) if k == "time/c/0" else s] for k, s in pm_doc["shard_sha256s"]
        ]
        pm_p.write_bytes(json_bytes(pm_doc))

        def publish() -> None:
            publish_cycle_receipt(
                cfg,
                store,
                product_id=prod_id,
                cycle_partition=part,
                attempt_id="att-2020",
                bundle=b,
                stage_store=stage,
            )

        with self.assertRaisesRegex(ValueError, "contradicts RunLock"):
            publish()
        coord_p.write_bytes(good_coord)
        pm_p.write_bytes(json_bytes(good_pm))
        with patch("av_wrf.runner.publish.datetime") as clock:
            clock.now.return_value = parse_iso_utc(stale_iso)
            sync = store._sync_object_parents

            def finish_after_deadline(path: Path) -> None:
                if path.name == "_ready.json":
                    clock.now.return_value += timedelta(days=1)
                sync(path)

            with patch.object(store, "_sync_object_parents", side_effect=finish_after_deadline):
                publish()
        self.assertIsNone(store.get_bytes(f"upstream/{b.product.model_id}/latest.json"))

    def test_rejects_corrupt_zstd_payload_with_valid_index_and_manifest_hash(self) -> None:
        prod_id, b, root = self.prod_id, self.bundle, self.root
        stage, dst = LocalFileStore(root / "stage"), LocalFileStore(root / "dst")
        attempt_id = "att-corrupt"
        origin = f"2026-11-12T{b.product.publish_origin_hour_utc:02d}:00:00Z"
        part = _stage_attempt_with_t2(self.cfg, stage, b, origin, attempt_id, 290.0)
        stage_prefix = attempt_stage_prefix(prod_id, part, attempt_id)
        shard = "pressure_pa/c/0/0/0/0"
        shard_file = root / "stage" / stage_prefix / "forecast.zarr" / shard
        valid_shard = shard_file.read_bytes()
        corrupt_shard = bytes([valid_shard[0] ^ 1]) + valid_shard[1:]
        shard_file.write_bytes(corrupt_shard)
        manifest_key = post_manifest_key(prod_id, part, attempt_id)
        corrupt_manifest = json.loads((root / "stage" / manifest_key).read_bytes())
        corrupt_manifest["shard_sha256s"] = [
            [key, sha256_hex(corrupt_shard) if key == shard else sha]
            for key, sha in corrupt_manifest["shard_sha256s"]
        ]
        (root / "stage" / manifest_key).write_bytes(json_bytes(corrupt_manifest))

        def publish(attempt: str) -> ReadyReceipt:
            return publish_cycle_receipt(
                self.cfg,
                dst,
                product_id=prod_id,
                cycle_partition=part,
                attempt_id=attempt,
                bundle=b,
                stage_store=stage,
            )

        with self.assertRaisesRegex(ValueError, "Corrupt Zstd payload"):
            publish(attempt_id)
        pub_root = f"upstream/{b.product.model_id}/{part}"
        early_key = f"{pub_root}/forecast.zarr/temp_2m_k/c/0/0/0"
        self.assertIsNone(dst.get_bytes(early_key))
        self.assertIsNone(dst.get_bytes(f"{pub_root}/_ready.json"))
        dst.put_if_absent(f"{pub_root}/_promotion_owner.json", b"legacy interrupted promotion")
        _stage_attempt_with_t2(self.cfg, stage, b, origin, "replacement", 292.0)
        self.assertEqual(publish("replacement").attempt_id, "replacement")
        self.assertIsNotNone(dst.get_bytes(early_key))
