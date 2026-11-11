"""Integration tests: interrupted promotion recovery, stale backfill latest.json, and coords."""

from __future__ import annotations

import dataclasses
import json
import shutil
import sys
import tempfile
import unittest
from collections.abc import Iterator
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
)
from av_wrf.core import resolve_cycle_window, selected_lead_hours  # noqa: E402
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
)


def _stage_fast_two_lead_cfg(src_cfg: Path, dst_cfg: Path) -> str:
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


def _seed_solver_receipt(
    store: LocalFileStore, b: LoadedBundle, cycle_partition: str, attempt_id: str, oci: str
) -> None:
    srec = SolverReceipt(
        b.product.product_id,
        cycle_partition,
        attempt_id,
        b.config_sha256,
        oci,
        "geog-sha",
        (("gfs.f000", "f0"),),
        (("wrfout_d01_00", "w0"),),
    )
    put_idempotent(
        store,
        solver_receipt_key(b.product.product_id, cycle_partition, attempt_id),
        json_bytes(srec.to_doc()),
    )


class TestPublishRecoveryAndBackfill(unittest.TestCase):
    def test_stale_backfill_and_tampered_coord_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = Path(tmp) / "store"
            store = LocalFileStore(store_dir)
            cfg = Path(tmp) / "cfg"
            prod_id = _stage_fast_two_lead_cfg(ROOT / "config", cfg)
            b = load_bundle(cfg, prod_id)
            manifest = BuildManifest("sha256:test", "wrf-sha", "wps-sha", (), "gcc", "openmpi")

            stale_iso = f"2020-01-01T{b.product.publish_origin_hour_utc:02d}:00:00Z"
            stale_win = resolve_cycle_window(b.product, b.output, stale_iso)
            plan_product_cycle(
                cfg,
                store,
                product_id=prod_id,
                publish_origin_utc=stale_iso,
                attempt_id="att-2020",
                mpi_ranks=4,
                build_manifest=manifest,
                bundle=b,
            )
            _seed_solver_receipt(store, b, stale_win.cycle_partition, "att-2020", "sha256:test")
            s_base = 1_577_836_800
            post_cycle_store(
                cfg,
                store,
                product_id=prod_id,
                cycle_partition=stale_win.cycle_partition,
                attempt_id="att-2020",
                f000_step=build_synthetic_wrf_baseline(b.domain, stale_iso, s_base),
                lead_steps=[
                    build_synthetic_wrf_step(b.domain, vt, s_base + h * 3600, h)
                    for h, vt in zip(stale_win.lead_hours, stale_win.valid_times_utc, strict=True)
                ],
                bundle=b,
            )
            stg_2020 = attempt_stage_prefix(prod_id, stale_win.cycle_partition, "att-2020")
            coord_p = store_dir / stg_2020 / "forecast.zarr" / "time/c/0"
            good_coord = coord_p.read_bytes()
            bad_coord = b"\x00" * len(good_coord)
            coord_p.write_bytes(bad_coord)
            pm_p = store_dir / post_manifest_key(prod_id, stale_win.cycle_partition, "att-2020")
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
                    cycle_partition=stale_win.cycle_partition,
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
                cycle_partition=stale_win.cycle_partition,
                attempt_id="att-2020",
                bundle=b,
                now_utc_iso="2026-11-12T00:30:00Z",
            )
            self.assertIsNone(store.get_bytes(f"upstream/{b.product.model_id}/latest.json"))

    def test_interrupted_promotion_recovery_and_pilot_smoke(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = Path(tmp) / "store"
            store = LocalFileStore(store_dir)
            cfg = Path(tmp) / "cfg"
            prod_id = _stage_fast_two_lead_cfg(ROOT / "config", cfg)
            b = load_bundle(cfg, prod_id)
            manifest = BuildManifest("sha256:test", "wrf-sha", "wps-sha", (), "gcc", "openmpi")
            origin_iso = f"2026-11-12T{b.product.publish_origin_hour_utc:02d}:00:00Z"
            win = resolve_cycle_window(b.product, b.output, origin_iso)
            orphan_key = (
                f"upstream/{b.product.model_id}/{win.cycle_partition}/"
                "forecast.zarr/temp_2m_k/c/0/0/0"
            )
            store.put_if_absent(orphan_key, b"uncommitted-partial-attempt-remnant")

            plan_product_cycle(
                cfg,
                store,
                product_id=prod_id,
                publish_origin_utc=origin_iso,
                attempt_id="att-02",
                mpi_ranks=4,
                build_manifest=manifest,
                bundle=b,
            )
            _seed_solver_receipt(store, b, win.cycle_partition, "att-02", "sha256:test")
            base_unix = 1_794_441_600
            ny, nx = b.domain.e_sn - 1, b.domain.e_we - 1

            def stream_leads() -> Iterator[RawWrfStepInput]:
                for h, vt in zip(win.lead_hours, win.valid_times_utc, strict=True):
                    step = build_synthetic_wrf_step(b.domain, vt, base_unix + h * 3600, h)
                    yield dataclasses.replace(
                        step, t2_k=CompMass2D(Slab2DF32.from_values(ny, nx, [296.0] * (ny * nx)))
                    )

            post_cycle_store(
                cfg,
                store,
                product_id=prod_id,
                cycle_partition=win.cycle_partition,
                attempt_id="att-02",
                f000_step=build_synthetic_wrf_baseline(b.domain, origin_iso, base_unix),
                lead_steps=stream_leads(),
                bundle=b,
            )
            rec = publish_cycle_receipt(
                cfg,
                store,
                product_id=prod_id,
                cycle_partition=win.cycle_partition,
                attempt_id="att-02",
                bundle=b,
                now_utc_iso="2026-11-12T00:30:00Z",
            )
            self.assertIsNotNone(store.get_bytes(f"upstream/{b.product.model_id}/latest.json"))
            stage_dir = store_dir / attempt_stage_prefix(prod_id, win.cycle_partition, "att-02")
            self.assertFalse(stage_dir.exists())
            self.assertEqual(
                rec,
                publish_cycle_receipt(
                    cfg,
                    store,
                    product_id=prod_id,
                    cycle_partition=win.cycle_partition,
                    attempt_id="att-02",
                    bundle=b,
                    now_utc_iso="2026-11-12T00:30:00Z",
                ),
            )
            diag_raw = store.get_bytes(
                f"upstream/{b.product.model_id}/{win.cycle_partition}/_diagnostics.json"
            )
            if diag_raw is None:
                self.fail("Expected published _diagnostics.json")
            diag = DiagnosticsReceipt.from_doc(json.loads(diag_raw.decode("utf-8")))
            self.assertLess(dict(diag.stage_seconds)["post"], 10.0)
            pub_t2 = store.get_bytes(orphan_key)
            if pub_t2 is None:
                self.fail("Expected recovered temp_2m_k shard")
            nj, ni = ny - 2 * b.domain.bdy_trim_cells, nx - 2 * b.domain.bdy_trim_cells
            decoded = decode_shard_f32(
                pub_t2, shard_time_steps=1, nz=1, ny=nj, nx=ni, output=b.output
            )
            self.assertAlmostEqual(decoded[0], 296.0, delta=0.05)


if __name__ == "__main__":
    unittest.main()
