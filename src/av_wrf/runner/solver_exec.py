"""Cold-start WPS (`geogrid`/`ungrib`/`metgrid`) and MPI WRF (`real`/`wrf`) execution."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

from av_wrf.codecs import validate_wrfinput_nc, verify_grid_against_domain
from av_wrf.contracts import BuildManifest, DomainProfile, RunLock, SolverReceipt, parse_iso_utc
from av_wrf.core import (
    compile_geogrid_namelist_sections,
    compile_wps_namelist_sections,
    compile_wrf_run_namelist,
    render_fortran_namelist,
    required_forcing_forecast_hours,
    validate_domain_profile,
)
from av_wrf.runner.bundle import LoadedBundle, load_bundle
from av_wrf.runner.mirrors import verify_forcing_manifest
from av_wrf.runner.solver import load_build_manifest, verify_solver_receipt
from av_wrf.runner.stages import load_run_lock
from av_wrf.runner.store import (
    ObjectStore,
    json_bytes,
    put_idempotent,
    sha256_file,
    solver_receipt_key,
)


def _resolve_solver_exe(solver_prefix: Path, exe_name: str) -> Path:
    """Resolve compiled executable strictly under `<solver_prefix>/bin/` (no PATH fallback)."""
    candidate = (solver_prefix / "bin" / exe_name).resolve()
    if (
        solver_prefix.resolve() not in candidate.parents
        or not candidate.is_file()
        or not os.access(candidate, os.X_OK)
    ):
        raise FileNotFoundError(
            f"Missing executable '{exe_name}' under {solver_prefix / 'bin'} (PATH fallback denied)"
        )
    return candidate


def _verify_runtime_build_manifest(
    solver_prefix: Path, lock: RunLock, runtime: BuildManifest
) -> None:
    installed = load_build_manifest(solver_prefix / "build-manifest.json", runtime.oci_digest)
    if installed != runtime or runtime != lock.build_manifest:
        raise ValueError("Installed solver and runtime build manifests must match the RunLock")


def _link_file(src: Path, dst: Path) -> None:
    if not src.is_file():
        raise FileNotFoundError(f"Missing required solver asset: {src}")
    os.symlink(src.resolve(), dst)


def _grib_alpha_suffix(index: int) -> str:
    if index < 0 or index >= 26**3:
        raise ValueError("Too many GRIB input files for 3-letter ungrib suffix")
    return f"{chr(65 + index // 676)}{chr(65 + (index // 26) % 26)}{chr(65 + index % 26)}"


def run_geogrid(
    config_root: Path,
    *,
    product_id: str,
    work_dir: Path,
    solver_prefix: Path = Path("/opt/av-wrf"),
    bundle: LoadedBundle | None = None,
) -> tuple[Path, str]:
    """Validate static geography, execute `geogrid.exe`, and write `geog_receipt.json`."""
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    validate_domain_profile(b.domain)
    geog_root = Path(b.domain.geog_data_path)
    if not geog_root.is_dir() or not any(geog_root.iterdir()):
        raise FileNotFoundError(f"Static WPS geography directory missing or empty: {geog_root}")
    exe = _resolve_solver_exe(solver_prefix, "geogrid.exe")
    geo_em = work_dir / "geo_em.d01.nc"
    work_dir.mkdir(parents=True, exist_ok=False)
    (work_dir / "geogrid").mkdir()
    _link_file(
        solver_prefix / "share" / "wps" / "GEOGRID.TBL.ARW", work_dir / "geogrid" / "GEOGRID.TBL"
    )
    wps_txt = render_fortran_namelist(compile_geogrid_namelist_sections(b.domain))
    (work_dir / "namelist.wps").write_text(wps_txt, encoding="utf-8")
    subprocess.run([str(exe)], cwd=work_dir, check=True)
    _validate_geo_em(geo_em, b.domain)
    geo_sha = sha256_file(geo_em)
    receipt = {
        "product_id": b.product.product_id,
        "domain_profile_id": b.domain.profile_id,
        "dem_source": b.domain.dem_source,
        "landuse_source": b.domain.landuse_source,
        "geo_em_sha256": geo_sha,
    }
    (work_dir / "geog_receipt.json").write_bytes(json_bytes(receipt))
    return geo_em, geo_sha


def _validate_geo_em(path: Path, domain: DomainProfile) -> None:
    import netCDF4

    with netCDF4.Dataset(str(path), mode="r") as ds:
        verify_grid_against_domain(ds, domain, path, wps=True)
        _verify_wps_values(ds.variables, path, ("HGT_M", "LU_INDEX", "LANDMASK"))


def _inspect_metgrid_dims(attempt_dir: Path, b: LoadedBundle, lock: RunLock) -> tuple[int, int]:
    import netCDF4

    init = parse_iso_utc(lock.forcing_cycle_utc)
    dates = tuple(
        (init + timedelta(hours=h)).strftime("%Y-%m-%d_%H:%M:%S")
        for h in required_forcing_forecast_hours(lock, b.primary_source)
    )
    files = tuple(attempt_dir / f"met_em.d01.{date}.nc" for date in dates)
    if set(attempt_dir.glob("met_em.d01.*.nc")) != set(files):
        raise RuntimeError("metgrid timestamps differ from the RunLock forcing window")
    dims_seen: set[tuple[int, int]] = set()
    for path, date in zip(files, dates, strict=True):
        with netCDF4.Dataset(str(path), mode="r") as ds:
            if bytes(ds["Times"][0].tobytes()).decode("ascii") != date:
                raise ValueError(f"met_em Times does not match filename: {path}")
            dims_seen.add(
                (len(ds.dimensions["num_metgrid_levels"]), len(ds.dimensions["num_st_layers"]))
            )
            _verify_wps_values(
                ds.variables,
                path,
                (
                    "PRES",
                    "GHT",
                    "TT",
                    "RH",
                    "UU",
                    "VV",
                    "PSFC",
                    "PMSL",
                    "SKINTEMP",
                    "SOILHGT",
                    "LANDSEA",
                    "SM",
                    "ST",
                    "SNOW",
                    "SEAICE",
                ),
            )
            verify_grid_against_domain(ds, b.domain, path, wps=True)
    if len(dims_seen) != 1:
        raise ValueError(f"Inconsistent met_em level dimensions across window: {dims_seen}")
    return next(iter(dims_seen))


def _verify_wps_values(
    variables: Mapping[str, object], path: Path, fields: tuple[str, ...]
) -> None:
    import netCDF4
    import numpy as np

    for name in fields:
        var = variables.get(name)
        if not isinstance(var, netCDF4.Variable):
            raise ValueError(f"Missing required WPS field {name} in {path}")
        values = var[:]
        if np.ma.is_masked(values) or not np.all(np.isfinite(values)) or np.any(abs(values) > 1e19):
            raise ValueError(f"Invalid values in WPS field {name} in {path}")


def _stage_wps_attempt(
    attempt_dir: Path,
    b: LoadedBundle,
    lock: RunLock,
    grib_dir: Path,
    geo_em: Path,
    solver_prefix: Path,
    forcing_shas: tuple[tuple[str, str], ...],
) -> None:
    vtable_name = b.primary_source.vtable_name
    _link_file(
        solver_prefix / "share" / "wps" / "Variable_Tables" / vtable_name, attempt_dir / "Vtable"
    )
    (attempt_dir / "metgrid").mkdir(exist_ok=True)
    _link_file(
        solver_prefix / "share" / "wps" / "METGRID.TBL.ARW", attempt_dir / "metgrid" / "METGRID.TBL"
    )
    _link_file(geo_em, attempt_dir / "geo_em.d01.nc")
    for idx, (fname, _) in enumerate(forcing_shas):
        _link_file(grib_dir / fname, attempt_dir / f"GRIBFILE.{_grib_alpha_suffix(idx)}")
    wps_txt = render_fortran_namelist(
        compile_wps_namelist_sections(b.domain, b.science, lock, b.primary_source)
    )
    (attempt_dir / "namelist.wps").write_text(wps_txt, encoding="utf-8")


def _exec_mpi_step(
    attempt_dir: Path, cmd: list[str], marker: str, *, reject_cfl: bool = False
) -> None:
    for rsl in attempt_dir.glob("rsl.*"):
        rsl.unlink()
    subprocess.run(cmd, cwd=attempt_dir, check=True)
    exe_name = Path(cmd[-1]).name
    rsl0 = attempt_dir / "rsl.error.0000"
    if not rsl0.is_file():
        raise RuntimeError(f"{exe_name} produced no rsl.error.0000 log")
    if marker not in rsl0.read_text(encoding="utf-8", errors="replace"):
        raise RuntimeError(f"{exe_name} missing completion marker '{marker}' in rsl.error.0000")
    if reject_cfl:
        for rsl_file in sorted(attempt_dir.glob("rsl.*")):
            txt = rsl_file.read_text(encoding="utf-8", errors="replace").lower()
            if "exceeded cfl" in txt or "points exceeded v_cfl" in txt:
                raise RuntimeError(f"Solver CFL violation detected in {rsl_file.name}")


def _link_run_tables(run_tables: Path, attempt_dir: Path) -> None:
    """Link WRF physics tables. Skip the real/wrf/ndown/tc binaries shipped in run/."""
    if not run_tables.is_dir():
        raise FileNotFoundError(f"Missing WRF runtime tables directory: {run_tables}")
    for item in sorted(run_tables.iterdir()):
        if (
            item.is_file()
            and not item.name.startswith("namelist")
            and item.name not in {"real", "wrf", "ndown", "tc"}
        ):
            _link_file(item, attempt_dir / item.name)


def _execute_solver_binaries(
    attempt_dir: Path,
    b: LoadedBundle,
    lock: RunLock,
    solver_prefix: Path,
    mpi_launcher: tuple[str, ...],
) -> tuple[tuple[str, str], ...]:
    subprocess.run(
        [str(_resolve_solver_exe(solver_prefix, "ungrib.exe"))], cwd=attempt_dir, check=True
    )
    subprocess.run(
        [str(_resolve_solver_exe(solver_prefix, "metgrid.exe"))], cwd=attempt_dir, check=True
    )
    mg_lvls, mg_soil = _inspect_metgrid_dims(attempt_dir, b, lock)
    _link_run_tables(solver_prefix / "share" / "wrf" / "run", attempt_dir)
    namelist = compile_wrf_run_namelist(
        b.domain,
        b.science,
        lock,
        b.primary_source,
        metgrid_levels=mg_lvls,
        metgrid_soil_levels=mg_soil,
        for_real=True,
    )
    namelist_path = attempt_dir / "namelist.input"
    namelist_path.write_text(render_fortran_namelist(namelist), encoding="utf-8")
    rank_cmd = [*mpi_launcher, str(lock.mpi_ranks)]
    _exec_mpi_step(
        attempt_dir,
        [*rank_cmd, str(_resolve_solver_exe(solver_prefix, "real.exe"))],
        "SUCCESS COMPLETE REAL_EM INIT",
    )
    validate_wrfinput_nc(attempt_dir / "wrfinput_d01", b.domain, b.science)
    integration = compile_wrf_run_namelist(
        b.domain,
        b.science,
        lock,
        b.primary_source,
        metgrid_levels=mg_lvls,
        metgrid_soil_levels=mg_soil,
    )
    namelist_path.write_text(render_fortran_namelist(integration), encoding="utf-8")
    _exec_mpi_step(
        attempt_dir,
        [*rank_cmd, str(_resolve_solver_exe(solver_prefix, "wrf.exe"))],
        "SUCCESS COMPLETE WRF",
        reject_cfl=True,
    )
    wrfouts = tuple(
        (p.name, sha256_file(p)) for p in sorted(attempt_dir.glob("wrfout_d01_*")) if p.is_file()
    )
    if not wrfouts:
        raise RuntimeError("wrf.exe completed without writing wrfout_d01_* files")
    return wrfouts


def run_wrf_cycle(
    config_root: Path,
    store: ObjectStore,
    *,
    product_id: str,
    cycle_partition: str,
    attempt_id: str,
    work_dir: Path,
    grib_dir: Path,
    geog_file: Path,
    runtime_manifest: BuildManifest,
    mpi_launcher: tuple[str, ...],
    solver_prefix: Path = Path("/opt/av-wrf"),
    bundle: LoadedBundle | None = None,
) -> SolverReceipt:
    """Execute `ungrib -> metgrid -> real -> wrf` in clean attempt scratch and bind receipt."""
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    lock = load_run_lock(store, product_id, cycle_partition, attempt_id)
    if lock.config_sha256 != b.config_sha256:
        raise ValueError("Config bundle SHA-256 drifted from frozen RunLock")
    _verify_runtime_build_manifest(solver_prefix, lock, runtime_manifest)
    root = work_dir.resolve()
    attempt_dir = root / product_id / cycle_partition.replace("/", "_") / attempt_id
    if attempt_dir.resolve() != attempt_dir:
        raise ValueError("Solver attempt directory must be an independent path under scratch")
    receipt_key = solver_receipt_key(product_id, cycle_partition, attempt_id)
    if store.get_bytes(receipt_key) is not None:
        return verify_solver_receipt(store, lock, attempt_dir)
    geo_sha = sha256_file(geog_file)
    if geo_sha != lock.geog_bundle_sha256:
        raise ValueError(f"geo_em.d01.nc SHA-256 {geo_sha} != RunLock {lock.geog_bundle_sha256}")
    _validate_geo_em(geog_file, b.domain)
    manifest = verify_forcing_manifest(grib_dir, lock, b.primary_source, domain=b.domain)
    attempt_dir.mkdir(parents=True, exist_ok=False)
    _stage_wps_attempt(
        attempt_dir, b, lock, grib_dir, geog_file, solver_prefix, manifest.file_sha256s
    )
    wrfouts = _execute_solver_binaries(attempt_dir, b, lock, solver_prefix, mpi_launcher)
    receipt = SolverReceipt(
        product_id,
        cycle_partition,
        attempt_id,
        lock.config_sha256,
        lock.build_manifest.oci_digest,
        geo_sha,
        manifest.file_sha256s,
        wrfouts,
    )
    put_idempotent(
        store,
        receipt_key,
        json_bytes(receipt.to_doc()),
    )
    return receipt
