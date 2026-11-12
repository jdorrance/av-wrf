"""Audit a receipt-backed forecast and saved WRF radiation with tiny local fixtures."""

import json
from pathlib import Path

import netCDF4
import numpy as np
import pytest
from scripts.audit_forecast import main

from av_wrf.contracts import BuildManifest, ReadyReceipt, SolverReceipt
from av_wrf.runner import (
    LocalFileStore,
    load_bundle,
    plan_product_cycle,
    post_cycle_from_inputs,
    publish_cycle_receipt,
    sha256_file,
    synthetic_cycle_prefix,
)
from tests.integration import stage_sparse_config


def _write_wrfout(path: Path, ready: ReadyReceipt, nx: int, ny: int, *, bad: bool) -> None:
    times = (ready.publish_origin_utc, *ready.valid_times_utc)
    with netCDF4.Dataset(path, mode="w") as ds:
        dims = ("Time", "south_north", "west_east")
        for dim, size in zip((*dims, "DateStrLen"), (len(times), ny, nx, 19), strict=True):
            ds.createDimension(dim, size)
        stamps = [stamp[:-1].replace("T", "_") for stamp in times]
        ds.createVariable("Times", "S1", ("Time", "DateStrLen"))[:] = np.asarray(
            [list(stamp) for stamp in stamps], dtype="S1"
        )
        for name, value in (
            ("ALBEDO", 0.3),
            ("SWDOWN", 12.0),
            ("SWNORM", 8.0),
            ("SWDNB", 20.0),
            ("SWUPB", 15.0),
            ("SWDNT", 100.0),
            ("SWUPT", 5.0),
        ):
            ds.createVariable(name, "f4", dims)[:] = value
        if bad:
            ds["ALBEDO"][1, ny // 2, nx // 2] = -9999.0
            ds["SWUPB"][1, ny // 2, nx // 2] = -199980.0


@pytest.fixture
def published_fixture(tmp_path: Path) -> tuple[ReadyReceipt, Path, int, int]:
    config = tmp_path / "config"
    product = stage_sparse_config(Path(__file__).resolve().parents[2] / "config", config)
    bundle = load_bundle(config, product)
    store_root = tmp_path / "store"
    store = LocalFileStore(store_root)
    _, lock = plan_product_cycle(
        config,
        store,
        product_id=product,
        publish_origin_utc=f"2026-11-12T{bundle.product.publish_origin_hour_utc:02d}:00:00Z",
        attempt_id="att-audit",
        mpi_ranks=1,
        bundle=bundle,
        build_manifest=BuildManifest("host-dev-unpinned", "wrf", "wps", (), "gcc", "mpi"),
    )
    identity = dict(
        product_id=product, cycle_partition=lock.cycle_partition, attempt_id=lock.attempt_id
    )
    post_cycle_from_inputs(
        config, store, **identity, wrfout_dir=None, synthetic=True, bundle=bundle
    )
    ready = publish_cycle_receipt(
        config,
        store,
        product_id=product,
        cycle_partition=lock.cycle_partition,
        attempt_id=lock.attempt_id,
        allow_synthetic=True,
        bundle=bundle,
    )
    root = store_root / synthetic_cycle_prefix(ready.model_id, ready.cycle_partition)
    return ready, root / "_ready.json", bundle.domain.e_we - 1, bundle.domain.e_sn - 1


def test_audit_reports_hashed_native_frames(
    tmp_path: Path, published_fixture: tuple[ReadyReceipt, Path, int, int]
) -> None:
    ready, ready_path, nx, ny = published_fixture
    wrfout, solver_path = tmp_path / "wrfout_d01_history", tmp_path / "solver_receipt.json"
    output = tmp_path / "audit.json"
    args = [
        str(ready_path),
        f"--baseline={ready_path}",
        f"--wrfout-dir={tmp_path}",
        f"--solver-receipt={solver_path}",
        f"--output={output}",
        "--require-clean-radiation",
    ]
    for bad in (False, True):
        _write_wrfout(wrfout, ready, nx, ny, bad=bad)
        receipt = SolverReceipt(
            ready.model_id,
            ready.cycle_partition,
            ready.attempt_id,
            ready.config_sha256,
            ready.oci_digest,
            "test-fixture-geog",
            (),
            ((wrfout.name, sha256_file(wrfout)),),
        )
        solver_path.write_text(json.dumps(receipt.to_doc()))
        assert main(args) == int(bad)
        report = json.loads(output.read_bytes())
        temperature = report["fields"]["temp_k"]
        assert temperature["max_absolute_difference"] == 0
        assert temperature["mean_absolute_difference_by_lead"] == [0.0] * len(ready.lead_hours)
        counts = report["radiation"]["published_interior"]
        assert counts["invalid_daylight_albedo"] == int(bad)
        assert counts["legacy_albedo_reflection"] == int(bad)
        assert counts["excess_surface_reflection"] == 0
    wrfout.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="Object digest differs"):
        main(args)
