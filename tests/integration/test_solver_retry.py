"""Recovery of interrupted native solver attempts."""

import signal
import subprocess
import sys
import time
from functools import partial
from pathlib import Path
from unittest.mock import patch

import pytest

from av_wrf.contracts import BuildManifest, ForcingManifest, SolverReceipt
from av_wrf.runner import LocalFileStore, plan_product_cycle, run_wrf_cycle
from av_wrf.runner.forcing_gfs import GFS
from av_wrf.runner.solver import _run_native
from av_wrf.runner.store import sha256_file
from tests.integration import bundle_for_recipe

CONFIG = Path(__file__).resolve().parents[2] / "config"
OUTPUT = "wrfout_d01_complete"


def _planned_retry(tmp_path: Path) -> tuple[partial[SolverReceipt], Path, ForcingManifest]:
    bundle = bundle_for_recipe(CONFIG, GFS)
    product = bundle.product
    source = bundle.primary_source
    origin = f"2026-11-12T{product.publish_origin_hour_utc:02d}:00:00Z"
    build = BuildManifest("sha256:" + "a" * 64, "w", "p", (), "g", "m")
    store = LocalFileStore(tmp_path / "store")
    _, lock = plan_product_cycle(
        CONFIG,
        store,
        product_id=product.product_id,
        publish_origin_utc=origin,
        attempt_id="retry",
        mpi_ranks=1,
        build_manifest=build,
        geog_bundle_sha256="b" * 64,
        bundle=bundle,
    )
    manifest = ForcingManifest(
        product.product_id,
        lock.cycle_partition,
        source.profile_id,
        lock.forcing_cycle_utc,
        source.vtable_name,
        source.interval_seconds,
        (0,),
        (("forcing.grib2", "c" * 64),),
    )
    run = partial(
        run_wrf_cycle,
        CONFIG,
        store,
        product_id=product.product_id,
        cycle_partition=lock.cycle_partition,
        attempt_id="retry",
        work_dir=tmp_path / "work",
        grib_dir=tmp_path / "forcing",
        geog_file=tmp_path / "geo",
        runtime_manifest=build,
        mpi_launcher=("mpirun", "-np"),
        bundle=bundle,
    )
    attempt = tmp_path / "work" / product.product_id / lock.cycle_partition.replace("/", "_")
    return run, attempt / "retry", manifest


def test_native_retry_recovers_output_after_receipt_write_failure(tmp_path: Path) -> None:
    run, final, manifest = _planned_retry(tmp_path)
    final.mkdir(parents=True)
    (final / "old.log").touch()
    calls: list[Path] = []

    def stage(scratch: Path, *_args: object) -> ForcingManifest:
        scratch.mkdir()
        return manifest

    def native(scratch: Path, *_args: object) -> tuple[tuple[str, float], ...]:
        calls.append(scratch)
        (scratch / "native.log").touch()
        if len(calls) == 1:
            raise RuntimeError("native failed")
        (scratch / OUTPUT).write_bytes(b"complete WRF output")
        return (("wrf", 1.5),)

    with (
        patch("av_wrf.runner.solver_exec._verify_runtime_build_manifest"),
        patch("av_wrf.runner.solver_exec._stage_wps_attempt", side_effect=stage),
        patch("av_wrf.runner.solver_exec._execute_solver_binaries", side_effect=native),
        patch(
            "av_wrf.runner.solver_exec._hash_complete_wrfout",
            side_effect=lambda p, *_: ((OUTPUT, sha256_file(p / OUTPUT)),),
        ),
    ):
        with pytest.raises(RuntimeError, match="native failed"):
            run()
        assert (calls[0] / "native.log").is_file()
        with (
            patch("av_wrf.runner.solver_exec.put_idempotent", side_effect=OSError("offline")),
            pytest.raises(OSError, match="offline"),
        ):
            run()
        receipt = run()
    assert len(calls) == 2
    assert receipt.wrfout_sha256s == ((OUTPUT, sha256_file(final / OUTPUT)),)
    assert dict(receipt.stage_seconds)["wrf"] == 1.5
    assert "inputs" in dict(receipt.stage_seconds)
    assert len(list(final.parent.glob(".retry.previous-*/work/old.log"))) == 1


def test_native_stage_times_out(tmp_path: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        _run_native([sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, 1)


@pytest.mark.parametrize("timeout", [None, 30])
def test_native_stage_forwards_sigterm(tmp_path: Path, timeout: int | None) -> None:
    launcher = (
        "import sys; from pathlib import Path; from av_wrf.runner.solver import _run_native; "
        f"_run_native([sys.executable, '-c', sys.argv[1]], Path.cwd(), {timeout!r})"
    )
    worker = (
        "import signal, sys, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, lambda *_: (Path('stopped').touch(), sys.exit(0))); "
        "Path('started').touch(); time.sleep(15)"
    )
    with subprocess.Popen([sys.executable, "-c", launcher, worker], cwd=tmp_path) as process:
        try:
            deadline = time.monotonic() + 5
            while not (tmp_path / "started").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert (tmp_path / "started").exists()
            process.send_signal(signal.SIGTERM)
            assert process.wait(timeout=10) == 128 + signal.SIGTERM
        finally:
            if process.poll() is None:
                process.kill()
    assert (tmp_path / "stopped").exists()
