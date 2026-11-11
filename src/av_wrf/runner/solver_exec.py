"""Cold-start WPS (`geogrid`/`ungrib`/`metgrid`) and MPI WRF (`real`/`wrf`) execution."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from av_wrf.contracts import BuildManifest, RunLock, SolverReceipt
from av_wrf.core import (
    compile_wps_namelist_sections,
    compile_wrf_run_namelist,
    render_fortran_namelist,
)
from av_wrf.runner.bundle import LoadedBundle, load_bundle, validate_bundle
from av_wrf.runner.stages import load_run_lock
from av_wrf.runner.store import (
    ObjectStore,
    json_bytes,
    put_idempotent,
    sha256_hex,
    solver_receipt_key,
)

PROVIDER_VTABLES: dict[str, str] = {
    "NOAA": "Vtable.GFS",
    "ECMWF": "Vtable.ECMWF",
    "DWD": "Vtable.ICONp",
}


def _hash_file(path: Path) -> str:
    return sha256_hex(path.read_bytes())


def _resolve_solver_exe(solver_prefix: Path, exe_name: str) -> Path:
    candidate = solver_prefix / "bin" / exe_name
    resolved = candidate.resolve() if candidate.exists() else Path(shutil.which(exe_name) or "")
    if not str(resolved) or not resolved.is_file():
        raise FileNotFoundError(
            f"Missing compiled solver executable '{exe_name}' under {solver_prefix}"
        )
    return resolved


def _link_file(src: Path, dst: Path) -> None:
    if not src.is_file():
        raise FileNotFoundError(f"Missing required solver asset: {src}")
    dst.unlink(missing_ok=True)
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
    validate_bundle(b)
    geog_root = Path(b.domain.geog_data_path)
    if not geog_root.is_dir() or not any(geog_root.iterdir()):
        raise FileNotFoundError(f"Static WPS geography directory missing or empty: {geog_root}")
    exe = _resolve_solver_exe(solver_prefix, "geogrid.exe")
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "geogrid").mkdir(exist_ok=True)
    _link_file(
        solver_prefix / "share" / "wps" / "GEOGRID.TBL.ARW", work_dir / "geogrid" / "GEOGRID.TBL"
    )
    dummy_lock = RunLock(
        b.product.product_id,
        b.product.model_id,
        "20000101/00",
        "geog",
        "2000-01-01T00:00:00Z",
        "2000-01-01T00:00:00Z",
        "2000-01-01T06:00:00Z",
        6,
        (1,),
        b.primary_source.profile_id,
        b.domain.profile_id,
        b.science.profile_id,
        b.output.profile_id,
        b.config_sha256,
        "geog",
        BuildManifest("geog", "w", "p", (), "gf", "mpi"),
        1,
    )
    wps_txt = render_fortran_namelist(
        compile_wps_namelist_sections(b.domain, b.science, dummy_lock)
    )
    (work_dir / "namelist.wps").write_text(wps_txt, encoding="utf-8")
    subprocess.run([str(exe)], cwd=work_dir, check=True)
    geo_em = work_dir / "geo_em.d01.nc"
    if not geo_em.is_file():
        raise RuntimeError("geogrid.exe exited 0 without producing geo_em.d01.nc")
    geo_sha = _hash_file(geo_em)
    receipt = {
        "product_id": b.product.product_id,
        "domain_profile_id": b.domain.profile_id,
        "dem_source": b.domain.dem_source,
        "landuse_source": b.domain.landuse_source,
        "geo_em_sha256": geo_sha,
    }
    (work_dir / "geog_receipt.json").write_bytes(json_bytes(receipt))
    return geo_em, geo_sha


def _inspect_metgrid_dims(attempt_dir: Path) -> tuple[int, int]:
    import netCDF4

    met_files = sorted(attempt_dir.glob("met_em.d01.*.nc"))
    if not met_files:
        raise RuntimeError("metgrid.exe produced no met_em.d01.*.nc files")
    with netCDF4.Dataset(str(met_files[0]), mode="r") as ds:
        return len(ds.dimensions["num_metgrid_levels"]), len(ds.dimensions["num_st_layers"])


def _stage_wps_attempt(
    attempt_dir: Path,
    b: LoadedBundle,
    lock: RunLock,
    grib_dir: Path,
    geo_em: Path,
    solver_prefix: Path,
) -> tuple[tuple[tuple[str, str], ...], str]:
    grib_files = [
        p for p in sorted(grib_dir.iterdir()) if p.is_file() and not p.name.startswith(".")
    ]
    if not grib_files:
        raise FileNotFoundError(f"No forcing GRIB files found in {grib_dir}")
    vtable_name = PROVIDER_VTABLES.get(b.primary_source.provider.upper(), "Vtable.GFS")
    _link_file(
        solver_prefix / "share" / "wps" / "Variable_Tables" / vtable_name, attempt_dir / "Vtable"
    )
    (attempt_dir / "metgrid").mkdir(exist_ok=True)
    _link_file(
        solver_prefix / "share" / "wps" / "METGRID.TBL.ARW", attempt_dir / "metgrid" / "METGRID.TBL"
    )
    _link_file(geo_em, attempt_dir / "geo_em.d01.nc")
    forcing_shas: list[tuple[str, str]] = []
    for idx, gp in enumerate(grib_files):
        _link_file(gp, attempt_dir / f"GRIBFILE.{_grib_alpha_suffix(idx)}")
        forcing_shas.append((gp.name, _hash_file(gp)))
    wps_txt = render_fortran_namelist(compile_wps_namelist_sections(b.domain, b.science, lock))
    (attempt_dir / "namelist.wps").write_text(wps_txt, encoding="utf-8")
    return tuple(forcing_shas), _hash_file(geo_em)


def _exec_mpi_step(attempt_dir: Path, exe: Path, ranks: int, marker: str) -> None:
    for rsl in attempt_dir.glob("rsl.*"):
        rsl.unlink()
    cmd = (
        ["mpirun", "--allow-run-as-root", "-np", str(ranks), str(exe)] if ranks > 1 else [str(exe)]
    )
    subprocess.run(cmd, cwd=attempt_dir, check=True)
    rsl0 = attempt_dir / "rsl.error.0000"
    if rsl0.is_file() and marker not in rsl0.read_text(encoding="utf-8", errors="replace"):
        raise RuntimeError(f"{exe.name} missing completion marker '{marker}' in rsl.error.0000")


def _execute_solver_binaries(
    attempt_dir: Path, b: LoadedBundle, lock: RunLock, solver_prefix: Path
) -> tuple[tuple[str, str], ...]:
    subprocess.run(
        [str(_resolve_solver_exe(solver_prefix, "ungrib.exe"))], cwd=attempt_dir, check=True
    )
    subprocess.run(
        [str(_resolve_solver_exe(solver_prefix, "metgrid.exe"))], cwd=attempt_dir, check=True
    )
    mg_lvls, mg_soil = _inspect_metgrid_dims(attempt_dir)
    run_tables = solver_prefix / "share" / "wrf" / "run"
    if not run_tables.is_dir():
        raise FileNotFoundError(f"Missing WRF runtime tables directory: {run_tables}")
    for item in sorted(run_tables.iterdir()):
        if item.is_file() and not item.name.startswith("namelist"):
            _link_file(item, attempt_dir / item.name)
    wrf_txt = render_fortran_namelist(
        compile_wrf_run_namelist(
            b.domain,
            b.science,
            lock,
            b.primary_source,
            metgrid_levels=mg_lvls,
            metgrid_soil_levels=mg_soil,
        )
    )
    (attempt_dir / "namelist.input").write_text(wrf_txt, encoding="utf-8")
    _exec_mpi_step(
        attempt_dir,
        _resolve_solver_exe(solver_prefix, "real.exe"),
        lock.mpi_ranks,
        "SUCCESS COMPLETE REAL_EM INIT",
    )
    _exec_mpi_step(
        attempt_dir,
        _resolve_solver_exe(solver_prefix, "wrf.exe"),
        lock.mpi_ranks,
        "SUCCESS COMPLETE WRF",
    )
    wrfouts = tuple(
        (p.name, _hash_file(p)) for p in sorted(attempt_dir.glob("wrfout_d01_*")) if p.is_file()
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
    geog_file: Path | None = None,
    solver_prefix: Path = Path("/opt/av-wrf"),
    bundle: LoadedBundle | None = None,
) -> SolverReceipt:
    """Execute `ungrib -> metgrid -> real -> wrf` in attempt scratch and persist SolverReceipt."""
    b = load_bundle(config_root, product_id) if bundle is None else bundle
    lock = load_run_lock(store, product_id, cycle_partition, attempt_id)
    if lock.config_sha256 != b.config_sha256:
        raise ValueError("Config bundle SHA-256 drifted from frozen RunLock")
    if lock.build_manifest.oci_digest == "host-dev-unpinned":
        raise ValueError("Refusing solve under host-dev-unpinned build manifest")
    attempt_dir = (work_dir / product_id / cycle_partition.replace("/", "_") / attempt_id).resolve()
    attempt_dir.mkdir(parents=True, exist_ok=True)
    geo_em = geog_file if geog_file is not None else (work_dir / "geo_em.d01.nc")
    if not geo_em.is_file():
        geo_em, _ = run_geogrid(
            config_root,
            product_id=product_id,
            work_dir=work_dir / "geog",
            solver_prefix=solver_prefix,
            bundle=b,
        )
    forcing_shas, geo_sha = _stage_wps_attempt(
        attempt_dir, b, lock, grib_dir, geo_em, solver_prefix
    )
    wrfouts = _execute_solver_binaries(attempt_dir, b, lock, solver_prefix)
    receipt = SolverReceipt(
        product_id,
        cycle_partition,
        attempt_id,
        lock.config_sha256,
        lock.build_manifest.oci_digest,
        geo_sha,
        forcing_shas,
        wrfouts,
    )
    put_idempotent(
        store,
        solver_receipt_key(product_id, cycle_partition, attempt_id),
        json_bytes(receipt.to_doc()),
    )
    return receipt
