"""Exercise grouped forcing acquisition through the source-neutral WPS stages."""

import json
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from functools import partial
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import netCDF4
import numpy as np
import yaml

from av_wrf.contracts import BuildManifest, DomainProfile, ForcingProfile, parse_iso_utc
from av_wrf.runner import (
    LocalFileStore,
    load_bundle,
    plan_product_cycle,
    prep_cycle_namelists,
    run_wrf_cycle,
    verify_forcing_manifest,
    write_forcing_manifest,
)
from av_wrf.runner.forcing_recipe import ForcingRecipe
from av_wrf.runner.solver_exec import _inspect_metgrid_dims
from av_wrf.runner.store import sha256_file, sha256_hex
from tests.integration import stage_sparse_config

CONFIG = Path(__file__).resolve().parents[2] / "config"
HOURS = (0, 3, 6, 9, 12)


def _check_window(
    _source: ForcingProfile, _domain: DomainProfile, _cycle: datetime, hours: tuple[int, ...]
) -> None:
    if hours != HOURS:
        raise ValueError("Fixture forcing window differs from its staged hours")


def _file_keys(cycle: datetime, hours: tuple[int, ...]) -> Mapping[str, tuple[str, ...]]:
    prefix = cycle.strftime("%Y%m%d/%H")
    return {str(hour): (f"{prefix}/{hour}/state", f"{prefix}/static") for hour in hours}


def _validate_file(
    path: Path, cycle: datetime, _source: ForcingProfile, _domain: DomainProfile, hour: int
) -> None:
    if path.read_text(encoding="ascii") != (cycle + timedelta(hours=hour)).isoformat():
        raise ValueError("Fixture bundle does not match its forecast hour")


RECIPE = ForcingRecipe(
    identity=("Fixture", "Vtable.fixture", 10_800, 0.5),
    check_window=_check_window,
    file_keys=_file_keys,
    validate_file=_validate_file,
    metgrid_fields=("SPECHUMD", "SNOWH", "SOIL_LEVELS", "SOILM"),
    metgrid_soil_dimensions=("num_soilt_levels", "num_soilm_levels"),
    vtable_text="fixture Vtable\n",
)


def _write_metgrid(
    path: Path, stamp: str, *, moisture_layers: int = 9, reported_soil_layers: int | None = 8
) -> None:
    with netCDF4.Dataset(str(path), "w") as ds:
        ds.createDimension("Time", 1)
        ds.createDimension("DateStrLen", 19)
        ds.createDimension("num_metgrid_levels", 5)
        ds.createDimension("num_soilt_levels", 8)
        ds.createDimension("num_soilm_levels", moisture_layers)
        if reported_soil_layers is not None:
            ds.setncattr("NUM_METGRID_SOIL_LEVELS", reported_soil_layers)
        ds.setncattr("FLAG_SOIL_LEVELS", 1)
        ds.setncattr("FLAG_SH", 1)
        ds.createVariable("Times", "S1", ("Time", "DateStrLen"))[0] = np.frombuffer(
            stamp.encode("ascii"), dtype="S1"
        )
        ds.createVariable("SPECHUMD", "f4", ("Time",))[:] = 0.005
        ds.createVariable("SNOWH", "f4", ("Time",))[:] = 0.1
        ds.createVariable("SOIL_LEVELS", "f4", ("num_soilt_levels",))[:] = np.arange(8)
        ds.createVariable("SOILM", "f4", ("Time",))[:] = 0.2


class TestForcingRecipes(TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory())).resolve()
        self.cfg = self.root / "config"
        self.product = stage_sparse_config(CONFIG, self.cfg)
        bundle = load_bundle(self.cfg, self.product)
        provider, vtable, interval, spacing = RECIPE.identity
        source = replace(
            bundle.primary_source,
            profile_id="fixture-grouped",
            provider=provider,
            vtable_name=vtable,
            interval_seconds=interval,
            grid_spacing_deg=spacing,
            mirrors=("https://example.invalid",),
            use_sh_qv=True,
        )
        source_path = self.cfg / "profiles/sources" / f"{source.profile_id}.yaml"
        source_path.write_text(yaml.safe_dump(asdict(source)), encoding="utf-8")
        product_path = self.cfg / "products" / f"{self.product}.yaml"
        product_path.write_text(
            product_path.read_text(encoding="utf-8").replace(
                f"primary_source_profile: {bundle.primary_source.profile_id}",
                f"primary_source_profile: {source.profile_id}",
            ),
            encoding="utf-8",
        )
        self.bundle = load_bundle(self.cfg, self.product)
        self.enterContext(patch.dict("av_wrf.runner.mirrors._RECIPES", {RECIPE.identity: RECIPE}))
        self.store = LocalFileStore(self.root / "store")
        self.geo = self.root / "geo_em.d01.nc"
        self.geo.write_bytes(b"verified geography")
        hour = self.bundle.product.publish_origin_hour_utc
        _, self.lock = plan_product_cycle(
            self.cfg,
            self.store,
            product_id=self.product,
            publish_origin_utc=f"2026-11-12T{hour:02d}:00:00Z",
            attempt_id="grouped",
            mpi_ranks=1,
            build_manifest=BuildManifest("sha256:" + "a" * 64, "w", "p", (), "g", "m"),
            geog_bundle_sha256=sha256_file(self.geo),
        )
        self.cycle = parse_iso_utc(self.lock.forcing_cycle_utc)

    def _assembled_files(self) -> dict[int, Path]:
        grib = self.root / "grib"
        grib.mkdir()
        files = {hour: grib / f"assembled.f{hour:03d}" for hour in reversed(HOURS)}
        for hour, path in files.items():
            path.write_text((self.cycle + timedelta(hours=hour)).isoformat(), encoding="ascii")
        return files

    def test_prep_and_manifest_keep_grouped_assets_and_seal_one_file_per_step(self) -> None:
        work = self.root / "prep"
        wps, wrf = prep_cycle_namelists(
            self.cfg,
            self.store,
            product_id=self.product,
            cycle_partition=self.lock.cycle_partition,
            attempt_id=self.lock.attempt_id,
            work_dir=work,
        )
        subset = json.loads((work / "forcing_subset.json").read_bytes())
        self.assertEqual(tuple(subset["forecast_hours"]), HOURS)
        prefix = self.cycle.strftime("%Y%m%d/%H")
        self.assertEqual(
            subset["file_keys"],
            {str(hour): [f"{prefix}/{hour}/state", f"{prefix}/static"] for hour in HOURS},
        )
        self.assertIn("interval_seconds = 10800", wps.read_text(encoding="utf-8"))
        self.assertIn("use_sh_qv = .true.", wrf.read_text(encoding="utf-8"))
        self.assertIn("use_sh_qv = .true.", (work / "namelist.input.real").read_text())

        grib, files = self.root / "grib", self._assembled_files()
        args = grib, self.lock, self.bundle.primary_source
        domain = self.bundle.domain
        with self.assertRaisesRegex(ValueError, "bundle does not match its forecast hour"):
            write_forcing_manifest(*args, {**files, 0: files[3], 3: files[0]}, domain=domain)
        file_shas = tuple((files[h].name, sha256_file(files[h])) for h in HOURS)
        inventory = grib / "sources.json"
        source_keys = {key for group in subset["file_keys"].values() for key in group}
        doc = {
            "forcing_cycle_utc": self.cycle.isoformat(),
            "source_sha256s": dict.fromkeys(source_keys, "a" * 64),
            "normalized_sha256s": {},
        }
        inventory.write_text(json.dumps(doc), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not match normalized forcing"):
            write_forcing_manifest(*args, files, domain=domain, acquisition_inventory=inventory)
        inventory.write_text(
            json.dumps({**doc, "normalized_sha256s": dict(file_shas)}), encoding="utf-8"
        )
        manifest = write_forcing_manifest(
            *args, files, domain=domain, acquisition_inventory=inventory
        )
        self.assertEqual(manifest.forcing_profile_id, self.bundle.primary_source.profile_id)
        self.assertEqual(manifest.forecast_hours, HOURS)
        self.assertEqual(manifest.file_sha256s, file_shas)
        self.assertEqual(manifest.acquisition_inventory, (inventory.name, sha256_file(inventory)))
        self.assertEqual(verify_forcing_manifest(*args, domain=domain), manifest)
        inventory.write_bytes(inventory.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "Acquisition inventory SHA-256 mismatch"):
            verify_forcing_manifest(*args, domain=domain)

    def test_solve_uses_verified_copies_and_retries_after_preflight_failure(self) -> None:
        grib, files = self.root / "grib", self._assembled_files()
        manifest = write_forcing_manifest(
            grib, self.lock, self.bundle.primary_source, files, domain=self.bundle.domain
        )
        table = self.root / "share/wps/METGRID.TBL.ARW"
        table.parent.mkdir(parents=True)
        table.touch()
        run = partial(
            run_wrf_cycle,
            self.cfg,
            self.store,
            product_id=self.product,
            cycle_partition=self.lock.cycle_partition,
            attempt_id=self.lock.attempt_id,
            work_dir=self.root / "work",
            grib_dir=grib,
            geog_file=self.geo,
            runtime_manifest=self.lock.build_manifest,
            mpi_launcher=("mpirun", "-np"),
            solver_prefix=self.root,
        )
        frame = "wrfout_d01_fixture"
        contents = tuple(files[hour].read_bytes() for hour in HOURS)

        def execute(attempt: Path, *_args: object) -> None:
            self.geo.write_bytes(b"changed geography")
            replacement = grib / "replacement"
            replacement.write_bytes(b"changed forcing")
            replacement.replace(files[0])
            self.assertEqual((attempt / "geo_em.d01.nc").read_bytes(), b"verified geography")
            self.assertEqual((attempt / "Vtable").read_bytes(), b"fixture Vtable\n")
            self.assertEqual(
                tuple(p.read_bytes() for p in sorted(attempt.glob("GRIBFILE.*"))), contents
            )
            (attempt / frame).touch()

        with (
            patch("av_wrf.runner.solver_exec._verify_runtime_build_manifest"),
            patch("av_wrf.runner.solver_exec._validate_geo_em"),
            patch(
                "av_wrf.runner.solver_exec._execute_solver_binaries", side_effect=execute
            ) as mock,
            patch(
                "av_wrf.runner.solver_exec._hash_complete_wrfout",
                return_value=((frame, sha256_hex(b"")),),
            ),
        ):
            files[HOURS[-1]].write_bytes(b"bad digest")
            with self.assertRaisesRegex(ValueError, "Forcing file SHA-256 mismatch"):
                run()
            mock.assert_not_called()
            self.assertFalse(list((self.root / "work").rglob(".inputs-*")))
            files[HOURS[-1]].write_bytes(contents[-1])
            receipt = run()
            self.assertEqual(receipt.forcing_sha256s, manifest.file_sha256s)
            self.assertEqual(receipt.geog_sha256, self.lock.geog_bundle_sha256)
            self.geo.unlink()
            self.assertEqual(run(), receipt)
            mock.assert_called_once()

    def test_metgrid_uses_recipe_fields_and_measured_soil_dimensions(self) -> None:
        for hour in HOURS:
            stamp = (self.cycle + timedelta(hours=hour)).strftime("%Y-%m-%d_%H:%M:%S")
            path = self.root / f"met_em.d01.{stamp}.nc"
            _write_metgrid(path, stamp)

        with patch("av_wrf.runner.solver_exec.verify_grid_against_domain"):
            self.assertEqual(_inspect_metgrid_dims(self.root, self.bundle, self.lock), (5, 8))
            _write_metgrid(path, stamp, moisture_layers=6)
            with self.assertRaisesRegex(ValueError, "Inconsistent met_em level dimensions"):
                _inspect_metgrid_dims(self.root, self.bundle, self.lock)
            _write_metgrid(path, stamp, moisture_layers=0)
            with self.assertRaisesRegex(ValueError, "Invalid met_em level dimensions"):
                _inspect_metgrid_dims(self.root, self.bundle, self.lock)
            for reported in (None, 1):
                _write_metgrid(path, stamp, reported_soil_layers=reported)
                with self.assertRaisesRegex(ValueError, "Invalid met_em NUM_METGRID_SOIL_LEVELS"):
                    _inspect_metgrid_dims(self.root, self.bundle, self.lock)
            _write_metgrid(path, stamp)
            with netCDF4.Dataset(str(path), "a") as ds:
                ds.delncattr("FLAG_SOIL_LEVELS")
            with self.assertRaisesRegex(ValueError, "Missing WPS FLAG_SOIL_LEVELS"):
                _inspect_metgrid_dims(self.root, self.bundle, self.lock)
            with netCDF4.Dataset(str(path), "a") as ds:
                ds.setncattr("FLAG_SOIL_LEVELS", 1)
                ds["SOILM"][:] = 20.0
            with self.assertRaisesRegex(ValueError, "Invalid values in WPS field SOILM"):
                _inspect_metgrid_dims(self.root, self.bundle, self.lock)
            _write_metgrid(path, stamp)
            with netCDF4.Dataset(str(path), "a") as ds:
                ds.delncattr("FLAG_SH")
            with self.assertRaisesRegex(ValueError, "Missing WPS FLAG_SH"):
                _inspect_metgrid_dims(self.root, self.bundle, self.lock)
            with netCDF4.Dataset(str(path), "a") as ds:
                ds.setncattr("FLAG_SH", 1)
                ds["SPECHUMD"][:] = 1.0
            with self.assertRaisesRegex(ValueError, "Invalid values in WPS field SPECHUMD"):
                _inspect_metgrid_dims(self.root, self.bundle, self.lock)
            with netCDF4.Dataset(str(path), "a") as ds:
                ds.renameVariable("SPECHUMD", "RH")
            with self.assertRaisesRegex(ValueError, "Missing required WPS field SPECHUMD"):
                _inspect_metgrid_dims(self.root, self.bundle, self.lock)
