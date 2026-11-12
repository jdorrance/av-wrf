"""Exercise grouped forcing acquisition through the source-neutral WPS stages."""

import json
from collections.abc import Mapping
from dataclasses import asdict, replace
from datetime import datetime, timedelta
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
    verify_forcing_manifest,
    write_forcing_manifest,
)
from av_wrf.runner.forcing_recipe import ForcingRecipe
from av_wrf.runner.solver_exec import _inspect_metgrid_dims, _stage_wps_attempt
from av_wrf.runner.store import sha256_file
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
    metgrid_fields=("SPECHUMD", "SNOWH"),
    metgrid_soil_dimensions=("num_soilt_levels", "num_soilm_levels"),
    vtable_text="fixture Vtable\n",
)


def _write_metgrid(path: Path, stamp: str, moisture_layers: int = 7) -> None:
    with netCDF4.Dataset(str(path), "w") as ds:
        ds.createDimension("Time", 1)
        ds.createDimension("DateStrLen", 19)
        ds.createDimension("num_metgrid_levels", 5)
        ds.createDimension("num_soilt_levels", 8)
        ds.createDimension("num_soilm_levels", moisture_layers)
        ds.createVariable("Times", "S1", ("Time", "DateStrLen"))[0] = np.frombuffer(
            stamp.encode("ascii"), dtype="S1"
        )
        ds.createVariable("SPECHUMD", "f4", ("Time",))[:] = 0.005
        ds.createVariable("SNOWH", "f4", ("Time",))[:] = 0.1


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
        hour = self.bundle.product.publish_origin_hour_utc
        _, self.lock = plan_product_cycle(
            self.cfg,
            self.store,
            product_id=self.product,
            publish_origin_utc=f"2026-11-12T{hour:02d}:00:00Z",
            attempt_id="grouped",
            mpi_ranks=1,
            build_manifest=BuildManifest("host-dev-unpinned", "w", "p", (), "g", "m"),
        )
        self.cycle = parse_iso_utc(self.lock.forcing_cycle_utc)

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

        grib = self.root / "grib"
        grib.mkdir()
        files = {hour: grib / f"assembled.f{hour:03d}" for hour in reversed(HOURS)}
        for hour, path in files.items():
            path.write_text((self.cycle + timedelta(hours=hour)).isoformat(), encoding="ascii")
        args = grib, self.lock, self.bundle.primary_source
        domain = self.bundle.domain
        with self.assertRaisesRegex(ValueError, "bundle does not match its forecast hour"):
            write_forcing_manifest(*args, {**files, 0: files[3], 3: files[0]}, domain=domain)
        manifest = write_forcing_manifest(*args, files, domain=domain)
        self.assertEqual(manifest.forcing_profile_id, self.bundle.primary_source.profile_id)
        self.assertEqual(manifest.forecast_hours, HOURS)
        self.assertEqual(
            manifest.file_sha256s, tuple((files[h].name, sha256_file(files[h])) for h in HOURS)
        )
        self.assertEqual(verify_forcing_manifest(*args, domain=domain), manifest)

        attempt = self.root / "attempt"
        attempt.mkdir()
        table = self.root / "share/wps/METGRID.TBL.ARW"
        table.parent.mkdir(parents=True)
        table.touch()
        geo = self.root / "geo_em.d01.nc"
        geo.touch()
        _stage_wps_attempt(
            attempt, self.bundle, self.lock, grib, geo, self.root, manifest.file_sha256s
        )
        self.assertEqual((attempt / "Vtable").read_bytes(), b"fixture Vtable\n")
        self.assertEqual(
            [link.resolve() for link in sorted(attempt.glob("GRIBFILE.*"))],
            [files[hour] for hour in HOURS],
        )

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
            _write_metgrid(path, stamp)
            with netCDF4.Dataset(str(path), "a") as ds:
                ds.renameVariable("SPECHUMD", "RH")
            with self.assertRaisesRegex(ValueError, "Missing required WPS field SPECHUMD"):
                _inspect_metgrid_dims(self.root, self.bundle, self.lock)
