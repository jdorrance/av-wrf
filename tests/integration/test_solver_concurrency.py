"""Same-attempt solver admission serialized by the local scratch flock alone."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import mkdtemp
from unittest.mock import patch

from av_wrf.contracts import BuildManifest, SolverReceipt
from av_wrf.runner import LocalFileStore, plan_product_cycle, run_wrf_cycle
from av_wrf.runner.forcing_gfs import GFS
from av_wrf.runner.store import _exclusive_lock, sha256_file, solver_receipt_key
from tests.integration import bundle_for_recipe

CONFIG = Path(__file__).resolve().parents[2] / "config"


def test_scratch_lock_serializes_same_attempt_without_remote_cycle_lock(tmp_path: Path) -> None:
    bundle = bundle_for_recipe(CONFIG, GFS)
    product = bundle.product

    class NoOpCycleStore(LocalFileStore):
        def cycle_lock(self, prefix: str) -> contextlib.AbstractContextManager[None]:
            # S3 and object stores release receipts through immutability, not a lease.
            return contextlib.nullcontext()

    store = NoOpCycleStore(tmp_path / "store")
    build = BuildManifest("sha256:" + "a" * 64, "w", "p", (), "g", "m")
    _, lock = plan_product_cycle(
        CONFIG,
        store,
        product_id=product.product_id,
        publish_origin_utc=f"2026-11-12T{product.publish_origin_hour_utc:02d}:00:00Z",
        attempt_id="concurrent",
        mpi_ranks=1,
        build_manifest=build,
        geog_bundle_sha256="b" * 64,
        bundle=bundle,
    )
    receipt_key = solver_receipt_key(product.product_id, lock.cycle_partition, lock.attempt_id)
    attempt_dir = (
        tmp_path
        / "work"
        / product.product_id
        / lock.cycle_partition.replace("/", "_")
        / lock.attempt_id
    )
    output = f"wrfout_d{lock.active_domain_id:02d}_complete"

    def run() -> SolverReceipt:
        return run_wrf_cycle(
            CONFIG,
            store,
            product_id=product.product_id,
            cycle_partition=lock.cycle_partition,
            attempt_id=lock.attempt_id,
            work_dir=tmp_path / "work",
            grib_dir=tmp_path / "forcing",
            geog_file=tmp_path / "geo",
            runtime_manifest=build,
            mpi_launcher=("mpirun", "-np"),
            bundle=bundle,
        )

    first_entered, release_first, second_attempting, second_acquired = (
        threading.Event() for _ in range(4)
    )
    solve_calls = 0
    seen_by_second: dict[str, bytes | bool | None] = {}

    def fake_solve(attempt: Path, *_args: object) -> tuple[Path, SolverReceipt]:
        nonlocal solve_calls
        solve_calls += 1
        assert solve_calls == 1, "second thread entered _solve_in_scratch under the same attempt"
        first_entered.set()
        assert release_first.wait(10), "first solve never released"
        workspace = Path(mkdtemp(prefix=f".{attempt.name}.solve-", dir=attempt.parent))
        scratch = workspace / "work"
        scratch.mkdir()
        (scratch / output).write_bytes(b"complete WRF output")
        receipt = SolverReceipt(
            lock.product_id,
            lock.cycle_partition,
            lock.attempt_id,
            lock.config_sha256,
            lock.build_manifest.oci_digest,
            lock.geog_bundle_sha256,
            (("forcing.grib2", "c" * 64),),
            ((output, sha256_file(scratch / output)),),
        )
        return scratch, receipt

    @contextlib.contextmanager
    def observed_lock(path: Path) -> Iterator[None]:
        if not threading.current_thread().name.startswith("second-solve"):
            with _exclusive_lock(path):
                yield
            return
        second_attempting.set()
        with _exclusive_lock(path):
            seen_by_second["receipt"] = store.get_bytes(receipt_key)
            seen_by_second["installed"] = attempt_dir.is_dir()
            second_acquired.set()
            yield

    with (
        patch("av_wrf.runner.solver_exec._verify_runtime_build_manifest"),
        patch("av_wrf.runner.solver_exec._solve_in_scratch", side_effect=fake_solve),
        patch("av_wrf.runner.solver_exec._exclusive_lock", observed_lock),
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="first-solve") as first_pool,
        ThreadPoolExecutor(max_workers=1, thread_name_prefix="second-solve") as second_pool,
    ):
        first = first_pool.submit(run)
        assert first_entered.wait(10), "first solve never entered scratch"
        second = second_pool.submit(run)
        assert second_attempting.wait(10), "second solve never reached the scratch lock"
        assert not second_acquired.wait(0.2), "second acquired the scratch lock while first ran"
        assert solve_calls == 1
        release_first.set()
        first_receipt = first.result(timeout=10)
        second_receipt = second.result(timeout=10)

    assert solve_calls == 1
    assert second_acquired.is_set()
    assert seen_by_second == {"receipt": store.get_bytes(receipt_key), "installed": True}
    assert seen_by_second["receipt"] is not None
    assert second_receipt == first_receipt
    assert (attempt_dir / output).read_bytes() == b"complete WRF output"
