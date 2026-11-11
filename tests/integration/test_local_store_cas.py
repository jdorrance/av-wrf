"""Integration tests: concurrent flock CAS, idempotent dispatch, YAML guards, and plan."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from av_wrf.contracts import BuildManifest, ReadyReceipt  # noqa: E402
from av_wrf.runner import (  # noqa: E402
    LocalFileStore,
    parse_declarative_yaml,
    plan_product_cycle,
    reconcile_dispatch,
)


class TestLocalFileStoreAndPlanIntegration(unittest.TestCase):
    def test_concurrent_cas_race_and_idempotent_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
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
            # Exactly one concurrent writer may win the generation-0 -> generation-1 CAS!
            self.assertEqual(outcomes.count(True), 1)
            self.assertEqual(outcomes.count(False), 7)

            calls: list[str] = []
            receipt = ReadyReceipt(
                schema_id="wrf-zarr-v3",
                schema_version=1,
                model_id="wrf-armenia-3km",
                cycle_partition="20261112/00",
                publish_origin_utc="2026-11-12T00:00:00Z",
                attempt_id="att-01",
                zarr_relpath="forecast.zarr",
                grid={"kind": "lambert-conformal"},
                exported_levels_k=40,
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
            def send_hook(rec: ReadyReceipt) -> int:
                calls.append(rec.attempt_id)
                return 202

            first = reconcile_dispatch(store, receipt, send_hook)
            second = reconcile_dispatch(store, receipt, send_hook)
            self.assertTrue(first)
            self.assertFalse(second)
            self.assertEqual(calls, ["att-01"])

    def test_strict_yaml_rejects_duplicates_templating_and_plan_locks_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            for bad_yaml in (
                "a: 1\na: 2\n",
                "a: ${SECRET_VAR}\n",
                "a: {{ jinja_var }}\n",
                "\ta: 1\n",
                "&anchor\na: 1\n",
            ):
                bad_file = tmp_path / "bad.yaml"
                bad_file.write_text(bad_yaml, encoding="utf-8")
                with self.assertRaises(ValueError):
                    parse_declarative_yaml(bad_file)

            store = LocalFileStore(tmp_path / "store")
            manifest = BuildManifest("sha256:test", "wrf-sha", "wps-sha", (), "gcc", "openmpi")
            lock_key, lock = plan_product_cycle(
                ROOT / "config",
                store,
                product_id="wrf-armenia-3km",
                publish_origin_utc="2026-11-12T00:00:00Z",
                attempt_id="att-01",
                mpi_ranks=16,
                build_manifest=manifest,
            )
            raw_bytes = store.get_bytes(lock_key)
            assert raw_bytes is not None
            parsed = json.loads(raw_bytes.decode("utf-8"))
            self.assertEqual(parsed["spinup_hours"], 6)
            self.assertEqual(lock.wrf_init_utc, "2026-11-11T18:00:00Z")
            with self.assertRaises(FileExistsError):
                plan_product_cycle(
                    ROOT / "config",
                    store,
                    product_id="wrf-armenia-3km",
                    publish_origin_utc="2026-11-12T00:00:00Z",
                    attempt_id="att-01",
                    mpi_ranks=16,
                    build_manifest=manifest,
                )


if __name__ == "__main__":
    unittest.main()
