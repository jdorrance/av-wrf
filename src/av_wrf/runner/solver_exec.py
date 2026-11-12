"""Cold-start WPS (`geogrid`/`ungrib`/`metgrid`) and MPI WRF (`real`/`wrf`) execution."""

from __future__ import annotations

import os
import re
import shutil
from datetime import timedelta
from numbers import Integral
from pathlib import Path
from tempfile import mkdtemp

from av_wrf.codecs import (
    check_geogrid_rotation,
    validate_wrfinput_nc,
    verify_grid_against_domain,
    verify_wps_fields,
)
from av_wrf.contracts import BuildManifest, DomainProfile, ForcingManifest, RunLock, SolverReceipt
from av_wrf.core import (
    compile_geogrid_namelist_sections,
    compile_wps_namelist_sections,
    compile_wrf_run_namelist,
    render_fortran_namelist,
    resolve_cycle_window,
    validate_domain_profile,
)
from av_wrf.runner.bundle import LoadedBundle, load_bundle
from av_wrf.runner.mirrors import resolve_forcing_window, verify_forcing_manifest
from av_wrf.runner.solver import (
    _hash_complete_wrfout,
    _install_solved_attempt,
    _link_file,
    _link_run_tables,
    _recover_solved_attempt,
    _resolve_solver_exe,
    _run_native,
    load_build_manifest,
    verify_solver_receipt,
)
from av_wrf.runner.stages import _verify_producer_sha256, load_run_lock
from av_wrf.runner.store import (
    ObjectStore,
    _copy_sha256_file,
    put_idempotent,
    sha256_file,
    solver_receipt_key,
)


def _verify_runtime_build_manifest(
    solver_prefix: Path, lock: RunLock, runtime: BuildManifest
) -> None:
    installed = load_build_manifest(solver_prefix / "build-manifest.json", runtime.oci_digest)
    if installed != runtime or runtime != lock.build_manifest:
        raise ValueError("Installed solver and runtime build manifests must match the RunLock")


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
    timeout_s: int | None = None,
    bundle: LoadedBundle | None = None,
) -> tuple[Path, str]:
    """Generate the validated static grid and return its path and SHA-256."""
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
    _run_native([str(exe)], work_dir, timeout_s)
    # WPS picks the first matching resolution for each field and logs the selected source.
    # https://github.com/wrf-model/WPS/blob/v4.7.0/geogrid/src/source_data_module.F#L802-L831
    log_path = work_dir / "geogrid.log"
    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.is_file() else ""
    selected = set(re.findall(r"Using (\S+) data source for (HGT_M|LANDUSEF)\.", log))
    expected = {(b.domain.dem_source, "HGT_M"), (b.domain.landuse_source, "LANDUSEF")}
    if selected != expected:
        geo_em.unlink(missing_ok=True)
        if not log.strip():
            raise RuntimeError(f"Missing or empty WPS geogrid.log: {log_path}")
        raise ValueError(f"Geogrid selected {sorted(selected)}, expected {sorted(expected)}")
    _validate_geo_em(geo_em, b.domain, repair_rotation=True)
    return geo_em, sha256_file(geo_em)


def _validate_geo_em(path: Path, domain: DomainProfile, *, repair_rotation: bool = False) -> None:
    import netCDF4

    with netCDF4.Dataset(
        str(path), mode="r+" if repair_rotation and domain.std_parallel_1_deg < 0.0 else "r"
    ) as ds:
        xlong = verify_grid_against_domain(ds, domain, path, wps=True)
        verify_wps_fields(ds, path, ("HGT_M", "LU_INDEX", "LANDMASK"))
        check_geogrid_rotation(ds, path, domain, xlong, repair=repair_rotation)


def _inspect_metgrid_dims(attempt_dir: Path, b: LoadedBundle, lock: RunLock) -> tuple[int, int]:
    import netCDF4

    recipe, cycle, hours = resolve_forcing_window(lock, b.primary_source, domain=b.domain)
    dates = tuple((cycle + timedelta(hours=h)).strftime("%Y-%m-%d_%H:%M:%S") for h in hours)
    files = tuple(attempt_dir / f"met_em.d01.{date}.nc" for date in dates)
    if set(attempt_dir.glob("met_em.d01.*.nc")) != set(files):
        raise RuntimeError("metgrid timestamps differ from the RunLock forcing window")
    dimensions = ("num_metgrid_levels", *recipe.metgrid_soil_dimensions)
    dims_seen: set[tuple[tuple[int, ...], int]] = set()
    for path, date in zip(files, dates, strict=True):
        with netCDF4.Dataset(str(path), mode="r") as ds:
            if bytes(ds["Times"][0].tobytes()).decode("ascii") != date:
                raise ValueError(f"met_em Times does not match filename: {path}")
            try:
                counts = tuple(len(ds.dimensions[name]) for name in dimensions)
            except KeyError as exc:
                raise ValueError(f"Missing met_em dimension {exc} in {path}") from exc
            # WPS reports the soil temperature count used by real.exe in this attribute.
            # https://github.com/wrf-model/WPS/blob/v4.7.0/metgrid/src/process_domain_module.F
            # https://github.com/wrf-model/WRF/blob/v4.8.0/share/input_wrf.F#L612-L640
            soil_levels: object = getattr(ds, "NUM_METGRID_SOIL_LEVELS", None)
            if not isinstance(soil_levels, Integral) or int(soil_levels) < 2:
                raise ValueError(f"Invalid met_em NUM_METGRID_SOIL_LEVELS in {path}")
            if counts[0] < 2 or min(counts) < 1:
                raise ValueError(f"Invalid met_em level dimensions in {path}: {counts}")
            dims_seen.add((counts, int(soil_levels)))
            verify_wps_fields(ds, path, recipe.metgrid_fields)
            verify_grid_against_domain(ds, b.domain, path, wps=True)
    if len(dims_seen) != 1:
        raise ValueError(f"Inconsistent met_em level dimensions across window: {dims_seen}")
    counts, wrf_soil_levels = dims_seen.pop()
    return counts[0], wrf_soil_levels


def _stage_wps_attempt(
    attempt_dir: Path,
    b: LoadedBundle,
    lock: RunLock,
    grib_dir: Path,
    geog_file: Path,
    solver_prefix: Path,
) -> ForcingManifest:
    source = b.primary_source
    recipe, _, _ = resolve_forcing_window(lock, source, domain=b.domain)
    inputs = attempt_dir / "inputs"
    inputs.mkdir(parents=True, mode=0o700)
    grib, geo_em = inputs / "grib", inputs / "geo_em.d01.nc"
    grib.mkdir()
    digest = _copy_sha256_file(geog_file, geo_em)
    if digest != lock.geog_bundle_sha256:
        raise ValueError(f"geo_em.d01.nc SHA-256 {digest} != RunLock {lock.geog_bundle_sha256}")
    _validate_geo_em(geo_em, b.domain)
    manifest = verify_forcing_manifest(grib_dir, lock, source, domain=b.domain, snapshot_dir=grib)

    vtable = attempt_dir / "Vtable"
    if recipe.vtable_text is None:
        _link_file(solver_prefix / "share" / "wps" / "Variable_Tables" / source.vtable_name, vtable)
    else:
        vtable.write_text(recipe.vtable_text, encoding="utf-8")
    (attempt_dir / "metgrid").mkdir(exist_ok=True)
    _link_file(
        solver_prefix / "share" / "wps" / "METGRID.TBL.ARW", attempt_dir / "metgrid" / "METGRID.TBL"
    )
    os.symlink("inputs/geo_em.d01.nc", attempt_dir / "geo_em.d01.nc")
    for idx, (fname, _) in enumerate(manifest.file_sha256s):
        os.symlink(f"inputs/grib/{fname}", attempt_dir / f"GRIBFILE.{_grib_alpha_suffix(idx)}")
    wps_txt = render_fortran_namelist(
        compile_wps_namelist_sections(b.domain, b.science, lock, source)
    )
    (attempt_dir / "namelist.wps").write_text(wps_txt, encoding="utf-8")
    return manifest


def _exec_mpi_step(
    attempt_dir: Path,
    cmd: list[str],
    marker: str,
    timeout_s: int | None,
    *,
    reject_cfl: bool = False,
) -> None:
    for rsl in attempt_dir.glob("rsl.*"):
        rsl.unlink()
    _run_native(cmd, attempt_dir, timeout_s)
    exe_name = Path(cmd[-1]).name
    rsl0 = attempt_dir / "rsl.error.0000"
    if not rsl0.is_file():
        raise RuntimeError(f"{exe_name} produced no rsl.error.0000 log")
    completed = False
    for log in sorted(attempt_dir.glob("rsl.*")) if reject_cfl else (rsl0,):
        with log.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if log == rsl0 and marker in line:
                    completed = True
                    if not reject_cfl:
                        break
                if reject_cfl:
                    lowered = line.lower()
                    if "exceeded cfl" in lowered or "points exceeded v_cfl" in lowered:
                        raise RuntimeError(f"Solver CFL violation detected in {log.name}")
    if not completed:
        raise RuntimeError(f"{exe_name} missing completion marker '{marker}' in rsl.error.0000")


def _execute_solver_binaries(
    attempt_dir: Path,
    b: LoadedBundle,
    lock: RunLock,
    solver_prefix: Path,
    mpi_launcher: tuple[str, ...],
    timeout_s: int | None,
) -> None:
    for executable in ("ungrib.exe", "metgrid.exe"):
        _run_native([str(_resolve_solver_exe(solver_prefix, executable))], attempt_dir, timeout_s)
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
        timeout_s,
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
        timeout_s,
        reject_cfl=True,
    )


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
    timeout_s: int | None = None,
    bundle: LoadedBundle | None = None,
) -> SolverReceipt:
    """Execute `ungrib -> metgrid -> real -> wrf` in clean attempt scratch and bind receipt."""
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    lock = load_run_lock(store, product_id, cycle_partition, attempt_id, bundle=b)
    _verify_runtime_build_manifest(solver_prefix, lock, runtime_manifest)
    attempt_dir = work_dir.resolve() / product_id / cycle_partition.replace("/", "_") / attempt_id
    if attempt_dir.resolve() != attempt_dir:
        raise ValueError("Solver attempt directory must be an independent path under scratch")
    receipt_key = solver_receipt_key(product_id, cycle_partition, attempt_id)
    with store.cycle_lock(receipt_key):
        if store.get_bytes(receipt_key) is not None:
            return verify_solver_receipt(store, lock, attempt_dir)
        _verify_producer_sha256(lock)
        if attempt_dir.exists() and not attempt_dir.is_dir():
            raise FileExistsError(f"Solver attempt path is not a directory: {attempt_dir}")
        recovered = _recover_solved_attempt(store, receipt_key, lock, attempt_dir)
        if recovered is not None:
            return recovered
        scratch, receipt = _solve_in_scratch(
            attempt_dir, b, lock, grib_dir, geog_file, solver_prefix, mpi_launcher, timeout_s
        )
        raw = _install_solved_attempt(scratch, attempt_dir, receipt)
        put_idempotent(store, receipt_key, raw)
        return receipt


def _solve_in_scratch(
    attempt_dir: Path,
    bundle: LoadedBundle,
    lock: RunLock,
    grib_dir: Path,
    geog_file: Path,
    solver_prefix: Path,
    mpi_launcher: tuple[str, ...],
    timeout_s: int | None,
) -> tuple[Path, SolverReceipt]:
    attempt_dir.parent.mkdir(parents=True, exist_ok=True)
    workspace = Path(mkdtemp(prefix=f".{attempt_dir.name}.solve-", dir=attempt_dir.parent))
    scratch = workspace / "work"
    try:
        manifest = _stage_wps_attempt(scratch, bundle, lock, grib_dir, geog_file, solver_prefix)
    except Exception:
        shutil.rmtree(workspace)
        raise
    try:
        _execute_solver_binaries(scratch, bundle, lock, solver_prefix, mpi_launcher, timeout_s)
    except Exception as exc:
        exc.add_note(f"Native solver logs are in {scratch}")
        raise
    window = resolve_cycle_window(bundle.product, bundle.output, lock.publish_origin_utc)
    receipt = SolverReceipt(
        lock.product_id,
        lock.cycle_partition,
        lock.attempt_id,
        lock.config_sha256,
        lock.build_manifest.oci_digest,
        lock.geog_bundle_sha256,
        manifest.file_sha256s,
        _hash_complete_wrfout(scratch, window),
        acquisition_inventory_sha256=(
            manifest.acquisition_inventory[1] if manifest.acquisition_inventory else None
        ),
    )
    return scratch, receipt
