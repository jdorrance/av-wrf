"""Real zarr-python v3 reader round-trip across padded shards and sparse time windows."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from array import array
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import numpy as np
import zarr

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from av_wrf.codecs import (  # noqa: E402
    build_synthetic_wrf_baseline,
    build_synthetic_wrf_step,
    encode_3d_shard_bytes,
)
from av_wrf.contracts import (  # noqa: E402
    WRF_ZARR_V3_2D_NAMES,
    WRF_ZARR_V3_3D_NAMES,
    BuildManifest,
    PubMass3D,
    RawWrfStepInput,
    Slab3DF32,
    parse_iso_utc,
)
from av_wrf.core import resolve_cycle_window  # noqa: E402
from av_wrf.runner import (  # noqa: E402
    LocalFileStore,
    attempt_stage_prefix,
    json_bytes,
    load_bundle,
    plan_product_cycle,
    post_cycle_from_inputs,
    post_manifest_key,
    publish_cycle_receipt,
    run_lock_key,
)
from av_wrf.runner.post import _stage_cycle  # noqa: E402
from av_wrf.runner.store import sha256_file  # noqa: E402
from tests.integration import stage_sparse_config  # noqa: E402


class TestZarrV3RoundTrip(unittest.TestCase):
    def test_publish_rejects_rehashed_nonmonotonic_height(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "config"
            b = load_bundle(cfg, stage_sparse_config(ROOT / "config", cfg))
            store = LocalFileStore(root / "store")
            _, lock = plan_product_cycle(
                cfg,
                store,
                product_id=b.product.product_id,
                publish_origin_utc=f"2026-11-12T{b.product.publish_origin_hour_utc:02d}:00:00Z",
                attempt_id="att-height",
                mpi_ranks=1,
                bundle=b,
                build_manifest=BuildManifest("host-dev-unpinned", "wrf", "wps", (), "gcc", "mpi"),
            )
            identity = dict(
                product_id=lock.product_id,
                cycle_partition=lock.cycle_partition,
                attempt_id=lock.attempt_id,
            )
            post_cycle_from_inputs(
                cfg, store, **identity, wrfout_dir=None, synthetic=True, bundle=b
            )
            staged = root / "store" / attempt_stage_prefix(**identity) / "forecast.zarr"
            height = zarr.open_group(str(staged), mode="r")["height_msl_m"]
            assert isinstance(height, zarr.Array)
            volume = np.asarray(height[0], dtype=np.float32)
            volume[1, 0, 0] = volume[0, 0, 0]
            buffer = array("f")
            buffer.frombytes(volume.tobytes())
            nz, ny, nx = volume.shape
            slab = PubMass3D(Slab3DF32(nz, ny, nx, buffer))
            shard_key = "height_msl_m/c/0/0/0/0"
            (staged / shard_key).write_bytes(encode_3d_shard_bytes([slab], b.output))
            pm = root / "store" / post_manifest_key(**identity)
            doc = json.loads(pm.read_bytes())
            doc["shard_sha256s"] = [
                [key, sha256_file(staged / key) if key == shard_key else sha]
                for key, sha in doc["shard_sha256s"]
            ]
            pm.write_bytes(json_bytes(doc))
            with self.assertRaisesRegex(ValueError, "Exported column gap"):
                publish_cycle_receipt(
                    cfg,
                    store,
                    product_id=lock.product_id,
                    cycle_partition=lock.cycle_partition,
                    attempt_id=lock.attempt_id,
                    allow_synthetic=True,
                    bundle=b,
                )

    def test_publish_checks_staged_producer_and_accepts_legacy_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = root / "config"
            product = stage_sparse_config(ROOT / "config", cfg)
            bundle = load_bundle(cfg, product)
            store_dir = root / "store"
            store = LocalFileStore(store_dir)
            origin = f"2026-11-12T{bundle.product.publish_origin_hour_utc:02d}:00:00Z"
            _, lock = plan_product_cycle(
                cfg,
                store,
                product_id=product,
                publish_origin_utc=origin,
                attempt_id="att-code",
                mpi_ranks=1,
                bundle=bundle,
                build_manifest=BuildManifest("host-dev-unpinned", "wrf", "wps", (), "gcc", "mpi"),
            )
            post_cycle_from_inputs(
                cfg,
                store,
                product_id=product,
                cycle_partition=lock.cycle_partition,
                attempt_id=lock.attempt_id,
                wrfout_dir=None,
                synthetic=True,
                bundle=bundle,
            )
            manifest = store_dir / post_manifest_key(product, lock.cycle_partition, lock.attempt_id)
            doc = json.loads(manifest.read_bytes())
            self.assertIsNotNone(lock.producer_sha256)
            self.assertEqual(doc["producer_sha256"], lock.producer_sha256)

            def publish() -> None:
                publish_cycle_receipt(
                    cfg,
                    store,
                    product_id=product,
                    cycle_partition=lock.cycle_partition,
                    attempt_id=lock.attempt_id,
                    bundle=bundle,
                    allow_synthetic=True,
                )

            with (
                patch("av_wrf.runner.stages._producer_sha256", return_value="0" * 64),
                self.assertRaisesRegex(ValueError, "Producer code SHA-256 drifted"),
            ):
                publish()
            manifest.write_bytes(json_bytes({**doc, "producer_sha256": "0" * 64}))
            with self.assertRaisesRegex(ValueError, "Staged producer SHA-256 differs"):
                publish()
            del doc["producer_sha256"]
            manifest.write_bytes(json_bytes(doc))
            with self.assertRaisesRegex(ValueError, "Staged producer SHA-256 differs"):
                publish()

            lock_path = store_dir / run_lock_key(product, lock.cycle_partition, lock.attempt_id)
            lock_doc = json.loads(lock_path.read_bytes())
            del lock_doc["producer_sha256"]
            lock_path.write_bytes(json_bytes(lock_doc))
            publish()
            manifest.write_bytes(json_bytes({**doc, "execution_mode": "solver"}))
            with (
                patch("av_wrf.runner.publish.verify_solver_receipt"),
                self.assertRaisesRegex(ValueError, "requires a producer SHA-256"),
            ):
                publish()

    def test_rejects_later_terrain_change_before_post_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tp, cfg = Path(tmp), Path(tmp) / "cfg"
            b = load_bundle(cfg, stage_sparse_config(ROOT / "config", cfg))
            store = LocalFileStore(tp / "store")
            orig = f"2026-11-12T{b.product.publish_origin_hour_utc:02d}:00:00Z"
            _, lock = plan_product_cycle(
                cfg,
                store,
                product_id=b.product.product_id,
                publish_origin_utc=orig,
                attempt_id="att-terrain",
                mpi_ranks=4,
                build_manifest=BuildManifest("host-dev-unpinned", "w", "p", (), "gcc", "mpi"),
                bundle=b,
            )
            win = resolve_cycle_window(b.product, b.output, orig)
            orig_s = int(parse_iso_utc(orig).timestamp())

            def leads() -> Iterator[RawWrfStepInput]:
                for i, (hour, time) in enumerate(
                    zip(win.lead_hours, win.valid_times_utc, strict=True)
                ):
                    step = build_synthetic_wrf_step(b.domain, time, orig_s + 3600 * hour, hour)
                    if i == 1:
                        step.terrain_m.buf[b.domain.bdy_trim_cells * (step.terrain_m.nx + 1)] += (
                            20.0
                        )
                    yield step

            with self.assertRaisesRegex(ValueError, "HGT changed since f000"):
                _stage_cycle(
                    store,
                    b,
                    lock,
                    win,
                    build_synthetic_wrf_baseline(b.domain, orig, orig_s),
                    leads(),
                    "synthetic",
                )
            self.assertIsNone(
                store.get_bytes(
                    post_manifest_key(lock.product_id, lock.cycle_partition, lock.attempt_id)
                )
            )

    def test_zarr_python_v3_reader_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tp, cfg = Path(tmp), Path(tmp) / "cfg"
            b = load_bundle(cfg, stage_sparse_config(ROOT / "config", cfg))
            store = LocalFileStore(tp / "store")
            bm = BuildManifest("host-dev-unpinned", "wrf-sha", "wps-sha", (), "gcc", "openmpi")
            orig = f"2026-11-12T{b.product.publish_origin_hour_utc:02d}:00:00Z"
            plan_product_cycle(
                cfg,
                store,
                product_id=b.product.product_id,
                publish_origin_utc=orig,
                attempt_id="att-zarr",
                mpi_ranks=4,
                build_manifest=bm,
                bundle=b,
            )
            win = resolve_cycle_window(b.product, b.output, orig)
            post_cycle_from_inputs(
                cfg,
                store,
                product_id=b.product.product_id,
                cycle_partition=win.cycle_partition,
                attempt_id="att-zarr",
                wrfout_dir=None,
                synthetic=True,
                bundle=b,
            )
            ready = publish_cycle_receipt(
                cfg,
                store,
                product_id=b.product.product_id,
                cycle_partition=win.cycle_partition,
                attempt_id="att-zarr",
                bundle=b,
                allow_synthetic=True,
                now_utc_iso="2026-11-12T00:15:00Z",
            )
            zarr_dir = (
                tp
                / "store"
                / "fixtures"
                / "synthetic"
                / b.product.model_id
                / win.cycle_partition
                / "forecast.zarr"
            )
            grp = zarr.open_group(str(zarr_dir), mode="r")
            self.assertEqual(ready.window_starts_utc, win.window_starts_utc)
            nj = b.domain.e_sn - 1 - 2 * b.domain.bdy_trim_cells
            ni = b.domain.e_we - 1 - 2 * b.domain.bdy_trim_cells
            n_t = len(win.lead_hours)
            for nm2 in WRF_ZARR_V3_2D_NAMES:
                node2 = grp[nm2]
                assert isinstance(node2, zarr.Array)
                arr2 = np.asarray(node2[:])
                self.assertEqual(arr2.shape, (nj, ni) if nm2 == "terrain_m" else (n_t, nj, ni))
                self.assertTrue(bool(np.all(np.isfinite(arr2))))
            for nm3 in WRF_ZARR_V3_3D_NAMES:
                node3 = grp[nm3]
                assert isinstance(node3, zarr.Array)
                arr3 = np.asarray(node3[:])
                self.assertEqual(arr3.shape, (n_t, b.domain.exported_levels_k, nj, ni))
                self.assertTrue(bool(np.all(np.isfinite(arr3))))

            starts, ends, precip = (
                grp[name] for name in ("window_start_unix_s", "window_end_unix_s", "precip_mm_hr")
            )
            assert isinstance(starts, zarr.Array)
            assert isinstance(ends, zarr.Array)
            assert isinstance(precip, zarr.Array)
            start_s = np.asarray(starts[:], dtype=np.int64)
            end_s = np.asarray(ends[:], dtype=np.int64)
            np.testing.assert_array_equal(start_s[1:], end_s[:-1])
            durations_h = (end_s - start_s) / 3600
            np.testing.assert_allclose(durations_h, (1, 1, 4))
            precip_rate = np.asarray(precip[:, 0, 0], dtype=np.float64)
            np.testing.assert_allclose(np.sum(durations_h * precip_rate), 6 * 0.25)
            cape = grp["cape_jkg"]
            assert isinstance(cape, zarr.Array)
            np.testing.assert_array_equal(cape[:], 320.0)


if __name__ == "__main__":
    unittest.main()
