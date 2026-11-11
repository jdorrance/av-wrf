"""Integration tests: CAS, provenance, ZEP-0002 publish gates, stage cleanup, and budget smoke."""

from __future__ import annotations

import json
import shutil
import struct
import sys
import tempfile
import unittest
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from av_wrf.codecs import build_synthetic_wrf_step, crc32c_u32, decode_shard_f32  # noqa: E402
from av_wrf.contracts import (  # noqa: E402
    BuildManifest,
    DiagnosticsReceipt,
    RawWrfStepInput,
    ReadyReceipt,
)
from av_wrf.core import resolve_cycle_window, selected_lead_hours  # noqa: E402
from av_wrf.runner import (  # noqa: E402
    LocalFileStore,
    attempt_stage_prefix,
    json_bytes,
    load_build_manifest,
    load_bundle,
    parse_declarative_yaml,
    plan_product_cycle,
    post_cycle_from_inputs,
    post_cycle_store,
    post_manifest_key,
    post_webhook_json,
    prep_cycle_namelists,
    publish_cycle_receipt,
    reconcile_dispatch,
    sha256_hex,
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


class TestLocalFileStoreAndPlanIntegration(unittest.TestCase):
    def test_concurrent_cas_provenance_fail_closed_and_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_p = Path(tmp)
            with self.assertRaises(FileNotFoundError):
                load_build_manifest(tmp_p / "missing.json", "")
            with self.assertRaises(FileNotFoundError):
                load_build_manifest(tmp_p / "missing.json", "sha256:abc", allow_unpinned_dev=True)
            dev_bm = load_build_manifest(tmp_p / "missing.json", "", allow_unpinned_dev=True)
            self.assertEqual(dev_bm.oci_digest, "host-dev-unpinned")

            store = LocalFileStore(tmp_p / "store")
            seed_sha = store.compare_and_swap("upstream/m/latest.json", b'{"gen":0}', None)

            def attempt_cas(worker_id: int) -> bool:
                try:
                    store.compare_and_swap(
                        "upstream/m/latest.json",
                        f'{{"gen":1,"w":{worker_id}}}'.encode(),
                        seed_sha,
                    )
                    return True
                except RuntimeError:
                    return False

            with ThreadPoolExecutor(max_workers=8) as pool:
                outcomes = list(pool.map(attempt_cas, range(8)))
            self.assertEqual((outcomes.count(True), outcomes.count(False)), (1, 7))

            receipt = ReadyReceipt(
                schema_id="wrf-zarr-v3",
                schema_version=1,
                model_id="wrf-synth-3km",
                cycle_partition="20261112/00",
                publish_origin_utc="2026-11-12T00:00:00Z",
                attempt_id="att-01",
                zarr_relpath="forecast.zarr",
                grid={"kind": "lambert-conformal"},
                exported_levels_k=38,
                lead_hours=(1, 2),
                valid_times_utc=("2026-11-12T01:00:00Z", "2026-11-12T02:00:00Z"),
                window_starts_utc=("2026-11-12T00:00:00Z", "2026-11-12T01:00:00Z"),
                window_ends_utc=("2026-11-12T01:00:00Z", "2026-11-12T02:00:00Z"),
                field_names=("terrain_m",),
                shard_sha256s=(("terrain_m/c/0/0", "abc"),),
                config_sha256="cfg123",
                forcing_profile_id="noaa-gfs-0p25-v1",
                oci_digest="sha256:test",
            )
            with self.assertRaises(ValueError):
                reconcile_dispatch(store, receipt, lambda r: post_webhook_json("", r))
            calls: list[str] = []

            def send_hook(rec: ReadyReceipt) -> int:
                calls.append(rec.attempt_id)
                return 202

            self.assertTrue(reconcile_dispatch(store, receipt, send_hook))
            self.assertFalse(reconcile_dispatch(store, receipt, send_hook))
            self.assertEqual(calls, ["att-01"])

    def test_timestamp_mismatch_malformed_manifest_and_corrupt_shard_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for bad_yaml in (
                "a: 1\na: 2\n",
                "a: ${SECRET_VAR}\n",
                "a: {{ jinja_var }}\n",
                "\ta: 1\n",
                "a: &anchor 1\nb: *anchor\n",
            ):
                bad_file = tmp_path / "bad.yaml"
                bad_file.write_text(bad_yaml, encoding="utf-8")
                with self.assertRaises(ValueError):
                    parse_declarative_yaml(bad_file)

            store = LocalFileStore(tmp_path / "store")
            cfg = tmp_path / "cfg"
            prod_id = _stage_fast_two_lead_cfg(ROOT / "config", cfg)
            b = load_bundle(cfg, prod_id)
            origin_iso = f"2026-11-12T{b.product.publish_origin_hour_utc:02d}:00:00Z"
            window = resolve_cycle_window(b.product, b.output, origin_iso)
            manifest = BuildManifest("sha256:test", "wrf-sha", "wps-sha", (), "gcc", "openmpi")
            _, lock = plan_product_cycle(
                cfg,
                store,
                product_id=prod_id,
                publish_origin_utc=origin_iso,
                attempt_id="att-bad",
                mpi_ranks=8,
                build_manifest=manifest,
            )
            wps_p, wrf_p = prep_cycle_namelists(
                cfg,
                store,
                product_id=prod_id,
                cycle_partition=lock.cycle_partition,
                attempt_id="att-bad",
                work_dir=tmp_path / "prep",
            )
            self.assertTrue(wps_p.is_file() and wrf_p.is_file())

            # Mismatched frame valid_time_utc fails immediately in post_cycle_store
            f000 = build_synthetic_wrf_step(b.domain, window.publish_origin_utc, 1_794_441_600, 0)
            wrong_t = build_synthetic_wrf_step(
                b.domain, window.valid_times_utc[1], 1_794_441_600 + 7200, 2
            )
            with self.assertRaises(ValueError):
                post_cycle_store(
                    cfg,
                    store,
                    product_id=prod_id,
                    cycle_partition=lock.cycle_partition,
                    attempt_id="att-bad",
                    f000_step=f000,
                    lead_steps=[wrong_t, wrong_t],
                )

            # Malformed post_manifest.json (non-list lead_hours / partial horizon) raises ValueError
            pm_key = post_manifest_key(prod_id, lock.cycle_partition, "att-bad")
            store.put_if_absent(pm_key, json_bytes({"lead_hours": "not-a-list"}))
            with self.assertRaises(ValueError):
                publish_cycle_receipt(
                    cfg,
                    store,
                    product_id=prod_id,
                    cycle_partition=lock.cycle_partition,
                    attempt_id="att-bad",
                )

            # Forged shard with matching SHA-256 in post_manifest.json is rejected by ZEP-0002 gate
            plan_product_cycle(
                cfg,
                store,
                product_id=prod_id,
                publish_origin_utc=origin_iso,
                attempt_id="att-corrupt",
                mpi_ranks=8,
                build_manifest=manifest,
            )
            post_cycle_from_inputs(
                cfg,
                store,
                product_id=prod_id,
                cycle_partition=lock.cycle_partition,
                attempt_id="att-corrupt",
                wrfout_dir=None,
                synthetic=True,
            )
            stage_root = attempt_stage_prefix(prod_id, lock.cycle_partition, "att-corrupt")
            terr_p = tmp_path / "store" / stage_root / "forecast.zarr" / "terrain_m/c/0/0"
            raw_s = bytearray(terr_p.read_bytes())
            struct.pack_into("<QQ", raw_s, len(raw_s) - 20, 4, 4)
            struct.pack_into("<I", raw_s, len(raw_s) - 4, crc32c_u32(bytes(raw_s[-20:-4])))
            terr_p.write_bytes(bytes(raw_s))
            pm_p = (
                tmp_path / "store" / post_manifest_key(prod_id, lock.cycle_partition, "att-corrupt")
            )
            pm_doc = json.loads(pm_p.read_text(encoding="utf-8"))
            pm_doc["shard_sha256s"] = [
                [k, sha256_hex(bytes(raw_s)) if k == "terrain_m/c/0/0" else s]
                for k, s in pm_doc["shard_sha256s"]
            ]
            pm_p.write_bytes(json_bytes(pm_doc))
            with self.assertRaises(ValueError):
                publish_cycle_receipt(
                    cfg,
                    store,
                    product_id=prod_id,
                    cycle_partition=lock.cycle_partition,
                    attempt_id="att-corrupt",
                )

    def test_full_pilot_horizon_smoke_budget_stage_cleanup_and_idempotent_retry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store_dir = Path(tmp) / "store"
            store = LocalFileStore(store_dir)
            cfg = ROOT / "config"
            bundles = [load_bundle(cfg, p.stem) for p in sorted((cfg / "products").glob("*.yaml"))]
            pilot = min(
                bundles,
                key=lambda b: len(selected_lead_hours(b.output)) * b.domain.e_we * b.domain.e_sn,
            )
            prod_id = pilot.product.product_id
            origin_iso = f"2026-11-12T{pilot.product.publish_origin_hour_utc:02d}:00:00Z"
            window = resolve_cycle_window(pilot.product, pilot.output, origin_iso)
            manifest = BuildManifest("sha256:test", "wrf-sha", "wps-sha", (), "gcc", "openmpi")
            plan_product_cycle(
                cfg,
                store,
                product_id=prod_id,
                publish_origin_utc=origin_iso,
                attempt_id="att-full",
                mpi_ranks=4,
                build_manifest=manifest,
                bundle=pilot,
            )
            yielded: list[int] = []
            base_unix = 1_794_441_600

            def stream_leads() -> Iterator[RawWrfStepInput]:
                for h, vt in zip(window.lead_hours, window.valid_times_utc, strict=True):
                    yielded.append(h)
                    yield build_synthetic_wrf_step(pilot.domain, vt, base_unix + h * 3600, h)

            f000 = build_synthetic_wrf_step(pilot.domain, window.publish_origin_utc, base_unix, 0)
            post_cycle_store(
                cfg,
                store,
                product_id=prod_id,
                cycle_partition=window.cycle_partition,
                attempt_id="att-full",
                f000_step=f000,
                lead_steps=stream_leads(),
                bundle=pilot,
            )
            self.assertEqual(tuple(yielded), window.lead_hours)

            rec = publish_cycle_receipt(
                cfg,
                store,
                product_id=prod_id,
                cycle_partition=window.cycle_partition,
                attempt_id="att-full",
                bundle=pilot,
            )
            # Staged directory must be removed once _ready.json + latest.json commit completes
            stage_dir = store_dir / attempt_stage_prefix(
                prod_id, window.cycle_partition, "att-full"
            )
            self.assertFalse(stage_dir.exists())
            # Retry of publish_cycle_receipt after stage cleanup remains idempotent
            rec_retry = publish_cycle_receipt(
                cfg,
                store,
                product_id=prod_id,
                cycle_partition=window.cycle_partition,
                attempt_id="att-full",
                bundle=pilot,
            )
            self.assertEqual(rec, rec_retry)

            diag_raw = store.get_bytes(
                f"upstream/{pilot.product.model_id}/{window.cycle_partition}/_diagnostics.json"
            )
            if diag_raw is None:
                self.fail("Expected published _diagnostics.json")
            diag = DiagnosticsReceipt.from_doc(json.loads(diag_raw.decode("utf-8")))
            post_seconds = dict(diag.stage_seconds)["post"]
            self.assertLess(post_seconds, 10.0)

            pub_sh = store.get_bytes(
                f"upstream/{pilot.product.model_id}/{window.cycle_partition}/"
                "forecast.zarr/sensible_heat_flux_wm2/c/0/0/0"
            )
            if pub_sh is None:
                self.fail("Expected published sensible_heat_flux_wm2 shard")
            nj = pilot.domain.e_sn - 1 - 2 * pilot.domain.bdy_trim_cells
            ni = pilot.domain.e_we - 1 - 2 * pilot.domain.bdy_trim_cells
            outer_t = min(len(window.lead_hours), pilot.output.shard_time_steps)
            decoded = decode_shard_f32(
                pub_sh, shard_time_steps=outer_t, nz=1, ny=nj, nx=ni, output=pilot.output
            )
            self.assertAlmostEqual(decoded[0], 240.0, delta=0.2)


if __name__ == "__main__":
    unittest.main()
