"""Native stage admission checks against WPS and WRF output."""

from dataclasses import replace
from datetime import timedelta
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import netCDF4
import numpy as np

from av_wrf.contracts import BuildManifest, CycleWindow
from av_wrf.core import resolve_cycle_window
from av_wrf.runner import (
    LocalFileStore,
    attempt_stage_prefix,
    load_bundle,
    plan_product_cycle,
    post_cycle_from_inputs,
    run_geogrid,
)
from av_wrf.runner.forcing_gfs import GFS
from av_wrf.runner.mirrors import resolve_forcing_window
from av_wrf.runner.solver import _hash_complete_wrfout
from av_wrf.runner.solver_exec import _inspect_metgrid_dims
from av_wrf.runner.store import sha256_file

CONFIG = Path(__file__).resolve().parents[2] / "config"


class TestSolverCompletion(TestCase):
    def test_geogrid_requires_resolved_sources_in_log(self) -> None:
        product = next((CONFIG / "products").glob("*.yaml")).stem
        default_log = (
            "2026-11-12 08:56:12.265 --- INFORM: Using default data source for HGT_M.\n"
            "2026-11-12 08:56:12.266 --- INFORM: Using default data source for LANDUSEF.\n"
        )
        log_error = RuntimeError, "Missing or empty WPS geogrid.log"
        cases = (
            ("default", default_log, None),
            (
                "missing-resolution",
                default_log,
                (ValueError, "Geogrid selected.*missing-resolution"),
            ),
            ("default", None, log_error),
            ("default", "", log_error),
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle = load_bundle(CONFIG, product)
            table = root / "share/wps/GEOGRID.TBL.ARW"
            table.parent.mkdir(parents=True)
            table.touch()
            for i, (dem, log, error) in enumerate(cases):
                domain = replace(bundle.domain, dem_source=dem, geog_data_path=tmp)
                work = root / f"work-{i}"
                geo = work / "geo_em.d01.nc"
                run = partial(
                    run_geogrid,
                    CONFIG,
                    product_id=product,
                    work_dir=work,
                    solver_prefix=root,
                    bundle=replace(bundle, domain=domain),
                )

                def fake_geogrid(
                    _args: list[str], *, cwd: Path, check: bool, log_text: str | None = log
                ) -> None:
                    self.assertTrue(check)
                    (cwd / "geo_em.d01.nc").write_bytes(b"mock geo_em")
                    if log_text is not None:
                        (cwd / "geogrid.log").write_text(log_text, encoding="utf-8")

                with (
                    self.subTest(dem=dem, log=log),
                    patch(
                        "av_wrf.runner.solver_exec._resolve_solver_exe", return_value=root / "bin"
                    ),
                    patch("av_wrf.runner.solver_exec.subprocess.run", side_effect=fake_geogrid),
                    patch("av_wrf.runner.solver_exec._validate_geo_em") as validate,
                ):
                    if error is None:
                        self.assertEqual(run(), (geo, sha256_file(geo)))
                        validate.assert_called_once_with(geo, domain, repair_rotation=True)
                    else:
                        with self.assertRaisesRegex(error[0], error[1]):
                            run()
                        self.assertFalse(geo.exists())
                        validate.assert_not_called()

    def test_gfs_metgrid_requires_temperature_and_pressure(self) -> None:
        product = next((CONFIG / "products").glob("*.yaml")).stem
        bundle = load_bundle(CONFIG, product)
        origin = f"2026-11-12T{bundle.product.publish_origin_hour_utc:02d}:00:00Z"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, lock = plan_product_cycle(
                CONFIG,
                LocalFileStore(root / "store"),
                product_id=product,
                publish_origin_utc=origin,
                attempt_id="test-metgrid",
                mpi_ranks=1,
                build_manifest=BuildManifest("host-dev-unpinned", "w", "p", (), "g", "m"),
                bundle=bundle,
            )
            lock = replace(lock, lead_hours=(1,))
            _, cycle, hours = resolve_forcing_window(
                lock, bundle.primary_source, domain=bundle.domain
            )
            for hour in hours:
                stamp = (cycle + timedelta(hours=hour)).strftime("%Y-%m-%d_%H:%M:%S")
                path = root / f"met_em.d01.{stamp}.nc"
                with netCDF4.Dataset(str(path), "w") as ds:
                    ds.createDimension("Time", 1)
                    ds.createDimension("DateStrLen", 19)
                    ds.createDimension("num_metgrid_levels", 2)
                    ds.createDimension("num_st_layers", 4)
                    ds.createDimension("num_sm_layers", 4)
                    ds.setncattr("NUM_METGRID_SOIL_LEVELS", 4)
                    times = ds.createVariable("Times", "S1", ("Time", "DateStrLen"))
                    times[0] = np.frombuffer(stamp.encode("ascii"), dtype="S1")
                    for field in set(GFS.metgrid_fields) | {"TT", "PRES"}:
                        ds.createVariable(field, "f4", ("Time",))[:] = 1.0
            with patch("av_wrf.runner.solver_exec.verify_grid_against_domain"):
                self.assertEqual(_inspect_metgrid_dims(root, bundle, lock), (2, 4))
                for field in ("TT", "PRES"):
                    with netCDF4.Dataset(str(path), "a") as ds:
                        ds.renameVariable(field, "unavailable")
                    with self.assertRaisesRegex(ValueError, f"Missing required WPS field {field}"):
                        _inspect_metgrid_dims(root, bundle, lock)
                    with netCDF4.Dataset(str(path), "a") as ds:
                        ds.renameVariable("unavailable", field)

    def test_receipt_requires_f000_and_every_lead(self) -> None:
        origin = "2026-11-12T00:00:00Z"
        end = "2026-11-12T01:00:00Z"
        window = CycleWindow(origin, origin, origin, "20261112/00", (1,), (end,), (origin,))
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = directory / "wrfout_d01_part.nc"
            with netCDF4.Dataset(str(path), "w") as ds:
                ds.createDimension("Time", None)
                ds.createDimension("DateStrLen", 19)
                times = ds.createVariable("Times", "S1", ("Time", "DateStrLen"))
                times[0, :] = np.frombuffer(b"2026-11-12_00:00:00", dtype="S1")
            with self.assertRaisesRegex(ValueError, "Missing wrfout lead timestamps"):
                _hash_complete_wrfout(directory, window)
            with netCDF4.Dataset(str(path), "a") as ds:
                ds.variables["Times"][1, :] = np.frombuffer(b"2026-11-12_01:00:00", dtype="S1")
            self.assertEqual(
                _hash_complete_wrfout(directory, window), ((path.name, sha256_file(path)),)
            )

    def test_public_post_requires_solver_receipt_before_staging(self) -> None:
        product = next((CONFIG / "products").glob("*.yaml")).stem
        bundle = load_bundle(CONFIG, product)
        origin = f"2026-11-12T{bundle.product.publish_origin_hour_utc:02d}:00:00Z"
        window = resolve_cycle_window(bundle.product, bundle.output, origin)
        stamps = (origin, *window.valid_times_utc)
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = LocalFileStore(root / "store")
            _, lock = plan_product_cycle(
                CONFIG,
                store,
                product_id=product,
                publish_origin_utc=origin,
                attempt_id="test-post",
                mpi_ranks=4,
                geog_bundle_sha256="a" * 64,
                build_manifest=BuildManifest("sha256:" + "b" * 64, "w", "p", (), "gcc", "mpi"),
                bundle=bundle,
            )
            with netCDF4.Dataset(str(root / "wrfout_d01_times.nc"), "w") as ds:
                ds.createDimension("Time", len(stamps))
                ds.createDimension("DateStrLen", 19)
                times = ds.createVariable("Times", "S1", ("Time", "DateStrLen"))
                raw = "".join(stamp[:-1].replace("T", "_") for stamp in stamps).encode("ascii")
                times[:] = np.frombuffer(raw, dtype="S1").reshape(len(stamps), 19)
            with self.assertRaisesRegex(FileNotFoundError, "Missing solver_receipt.json"):
                post_cycle_from_inputs(
                    CONFIG,
                    store,
                    product_id=product,
                    cycle_partition=lock.cycle_partition,
                    attempt_id=lock.attempt_id,
                    wrfout_dir=root,
                    bundle=bundle,
                )
            stage = attempt_stage_prefix(product, lock.cycle_partition, lock.attempt_id)
            self.assertFalse((root / "store" / stage).exists())
