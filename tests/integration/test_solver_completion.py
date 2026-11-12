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
    json_bytes,
    load_bundle,
    load_run_lock,
    plan_product_cycle,
    post_cycle_from_inputs,
    run_geogrid,
    run_wrf_cycle,
)
from av_wrf.runner.forcing_gfs import GFS
from av_wrf.runner.mirrors import resolve_forcing_window
from av_wrf.runner.solver import _hash_complete_wrfout, parse_build_manifest
from av_wrf.runner.solver_exec import _inspect_metgrid_dims
from av_wrf.runner.store import sha256_file
from tests.integration import bundle_for_recipe

CONFIG = Path(__file__).resolve().parents[2] / "config"


class TestSolverCompletion(TestCase):
    def test_solve_and_post_require_planned_producer_code(self) -> None:
        bundle = bundle_for_recipe(CONFIG, GFS)
        product = bundle.product
        build = BuildManifest("host-dev-unpinned", "wrf", "wps", (), "gcc", "mpi")
        attempt = "code-drift"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = LocalFileStore(root)
            plan = partial(
                plan_product_cycle,
                CONFIG,
                store,
                product_id=product.product_id,
                publish_origin_utc=f"2026-11-12T{product.publish_origin_hour_utc:02d}:00:00Z",
                attempt_id=attempt,
                mpi_ranks=4,
                build_manifest=build,
                bundle=bundle,
            )
            key, lock = plan()
            self.assertRegex(lock.producer_sha256 or "", r"^[0-9a-f]{64}$")
            stages = (
                partial(
                    run_wrf_cycle,
                    work_dir=root / "solve",
                    grib_dir=root / "grib",
                    geog_file=root / "missing-geog.nc",
                    runtime_manifest=build,
                    mpi_launcher=("mpirun", "-np"),
                ),
                partial(post_cycle_from_inputs, wrfout_dir=root / "missing"),
            )
            with (
                patch("av_wrf.runner.stages._producer_sha256", return_value="0" * 64),
                patch("av_wrf.runner.solver_exec._verify_runtime_build_manifest"),
            ):
                for stage in stages:
                    with self.assertRaisesRegex(ValueError, "Producer code SHA-256 drifted"):
                        stage(
                            CONFIG,
                            store,
                            product_id=product.product_id,
                            cycle_partition=lock.cycle_partition,
                            attempt_id=attempt,
                            bundle=bundle,
                        )
            self.assertFalse((root / "solve").exists())
            doc = lock.to_doc()
            del doc["producer_sha256"]
            (root / key).write_bytes(json_bytes(doc))
            self.assertEqual(plan()[0], key)
            legacy = load_run_lock(
                store, product.product_id, lock.cycle_partition, attempt, bundle=bundle
            )
            self.assertIsNone(legacy.producer_sha256)

    def test_plan_rejects_invalid_native_mpi_mesh(self) -> None:
        bundle = bundle_for_recipe(CONFIG, GFS)
        product = bundle.product
        origin = f"2026-11-12T{product.publish_origin_hour_utc:02d}:00:00Z"
        with TemporaryDirectory() as tmp:
            store = LocalFileStore(Path(tmp))
            plan = partial(
                plan_product_cycle,
                CONFIG,
                store,
                product_id=product.product_id,
                publish_origin_utc=origin,
                attempt_id="mpi-guard",
                build_manifest=BuildManifest("host-dev-unpinned", "w", "p", (), "g", "m"),
                bundle=bundle,
            )
            with self.assertRaisesRegex(ValueError, "patch narrower than 10 cells"):
                plan(mpi_ranks=37)
            with self.assertRaisesRegex(ValueError, "exceeds WRF's 10-cell"):
                plan(mpi_ranks=bundle.domain.e_we * bundle.domain.e_sn)
            self.assertEqual(plan(mpi_ranks=4)[1].mpi_ranks, 4)

    def test_native_manifest_validates_commits_and_preserves_legacy_builds(self) -> None:
        image = "sha256:" + "a" * 64
        manifest = {
            "wrf_sha": "1" * 40,
            "wps_sha": "2" * 40,
            "patch_sha256s": [],
            "compiler": "gcc",
            "mpi": "openmpi",
        }
        self.assertIsNone(parse_build_manifest(manifest, image).jasper_git_sha)
        for key in ("wrf_sha", "wps_sha", "jasper_sha"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, f"Invalid.*{key}"):
                parse_build_manifest({**manifest, key: "unverified"}, image)
        locked = parse_build_manifest({**manifest, "jasper_sha": "3" * 40}, image)
        self.assertEqual(locked.jasper_git_sha, "3" * 40)
        self.assertEqual(BuildManifest.from_doc(locked.to_doc()), locked)

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
        bundle = bundle_for_recipe(CONFIG, GFS)
        origin = f"2026-11-12T{bundle.product.publish_origin_hour_utc:02d}:00:00Z"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, lock = plan_product_cycle(
                CONFIG,
                LocalFileStore(root / "store"),
                product_id=bundle.product.product_id,
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
