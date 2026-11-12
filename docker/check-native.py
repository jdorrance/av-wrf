"""Run pinned WPS and WRF through native history on synthetic inputs."""

import dataclasses
import subprocess
import sys
from pathlib import Path

import netCDF4
import numpy as np
from native_fixture import forcing, geography

from av_wrf.codecs import read_wrfout_baseline_nc, read_wrfout_step_nc
from av_wrf.contracts import BuildManifest, DomainProfile, RunLock, ScienceProfile
from av_wrf.core import (
    MYNN_HISTORY_FILE,
    MYNN_HISTORY_TEXT,
    build_accum_baseline,
    compile_wps_namelist_sections,
    compile_wrf_run_namelist,
    post_frame,
    render_fortran_namelist,
    validate_frame_columns,
)
from av_wrf.runner import load_bundle

PREFIX = Path("/opt/av-wrf")
START = "2026-01-15_08:00:00"
END = "2026-01-15_11:00:00"


def _run(work: Path, program: str, marker: str, mpi: bool = False) -> None:
    cmd = [str(PREFIX / "bin" / f"{program}.exe")]
    if mpi:
        cmd = ["mpirun", "--allow-run-as-root", "-np", "1", *cmd]
    result = subprocess.run(cmd, cwd=work, capture_output=True, text=True, timeout=300, check=False)
    (work / f"{program}.stdout").write_text(result.stdout + result.stderr, encoding="utf-8")
    logs = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (work / f"{program}.log", work / "rsl.error.0000", work / "rsl.out.0000")
        if path.exists()
    )
    if result.returncode or marker not in logs + result.stdout + result.stderr:
        raise RuntimeError(
            f"{program} failed ({result.returncode}); inspect logs in {work}\n"
            f"{result.stdout}\n{result.stderr}\n{logs}"
        )
    print(f"{program}: {marker}", flush=True)


def _metgrid_output(work: Path, domains: int) -> None:
    expected_paths = {
        work / f"met_em.d{domain:02d}.{date}.nc"
        for domain in range(1, domains + 1)
        for date in ((START, END) if domain == 1 else (START,))
    }
    if set(work.glob("met_em.d*.nc")) != expected_paths:
        raise ValueError("Expected both parent inputs and one child initialization")
    for path in sorted(expected_paths):
        with netCDF4.Dataset(path) as ds:
            if len(ds.dimensions["num_metgrid_levels"]) != 15:
                raise ValueError(f"Incorrect pressure-level count: {path}")
            if int(ds.NUM_METGRID_SOIL_LEVELS) != 4 or int(ds.FLAG_SH) != 1:
                raise ValueError(f"Missing specific humidity or four soil levels: {path}")
            for name, expected in (("PSFC", 100000), ("SPECHUMD", 0.006), ("SM", 0.25)):
                data = np.asarray(ds[name][:])
                if not np.isfinite(data).all() or not np.any(np.isclose(data, expected)):
                    raise ValueError(f"Unexpected {name} decoded by metgrid: {path}")
    print(f"met_em: {domains} domain(s), FLAG_SH=1, 15 air and 4 soil levels", flush=True)


def _check_history(
    work: Path, domain: DomainProfile, science: ScienceProfile, domains: int
) -> None:
    for domain_id in range(1, domains + 1):
        if not (work / f"wrfinput_d{domain_id:02d}").is_file():
            raise RuntimeError("Missing native WRF initialization output")
    stamps = [f"{START[:14]}{minute:02d}:00" for minute in (0, 1, 2)]
    paths = [work / f"wrfout_d{domains:02d}_{stamp}" for stamp in stamps]
    if set(paths) != set(work.glob("wrfout_d*")):
        raise RuntimeError("Expected three exact history times on the published domain")
    raw = read_wrfout_baseline_nc(paths[0], domain, science=science)
    baseline = build_accum_baseline(raw, domain.bdy_trim_cells)
    for path, stamp in zip(paths[1:], stamps[1:], strict=True):
        step = read_wrfout_step_nc(
            path,
            domain,
            science,
            expected_valid_utc=stamp.replace("_", "T") + "Z",
            require_surface_shortwave=True,
            require_mynn_clouds=True,
        )
        frame, baseline = post_frame(baseline, step, domain, science)
        validate_frame_columns(frame, domain)
        if step.dt_s is None or frame.window_end_unix_s - frame.window_start_unix_s != 60:
            raise ValueError("Expected native adaptive DT and a one-minute accumulation window")
    print(f"history: d{domains:02d} exact minute times, MYNN and column checks", flush=True)


def _fixture(work: Path, nested: bool) -> None:
    candidates = (
        load_bundle(Path("config"), p.stem) for p in Path("config/products").glob("*.yaml")
    )
    bundle = next(b for b in candidates if b.candidate_source.use_sh_qv)
    source = bundle.candidate_source
    domain = dataclasses.replace(
        bundle.domain,
        e_we=103,
        e_sn=103,
        geog_data_path=str(work / "geog"),
        parent_grid_ratio=3 if nested else 1,
        parent_halo_cells=10 if nested else 0,
        nest_trigger_ratio=2.0,
    )
    science = dataclasses.replace(bundle.science, history_interval_min=1)
    init = START.replace("_", "T") + "Z"
    lock = RunLock(
        bundle.product.product_id,
        bundle.product.model_id,
        "20260115/08",
        "native-fixture",
        init,
        init,
        init,
        0,
        (1,),
        source.profile_id,
        domain.profile_id,
        science.profile_id,
        bundle.output.profile_id,
        "fixture",
        "fixture",
        BuildManifest("", "", "", (), "", ""),
        1,
        max_dom=2 if nested else 1,
        active_domain_id=2 if nested else 1,
    )
    geography(work / "geog")
    (work / "geogrid").mkdir()
    (work / "metgrid").mkdir()
    (work / "geogrid" / "GEOGRID.TBL").symlink_to(work / "geog" / "GEOGRID.TBL")
    (work / "metgrid" / "METGRID.TBL").symlink_to(PREFIX / "share/wps/METGRID.TBL.ARW")
    wps = compile_wps_namelist_sections(domain, science, lock, source)
    (work / "namelist.wps").write_text(render_fortran_namelist(wps), encoding="utf-8")
    _run(work, "geogrid", "Successful completion of geogrid")
    forcing(work, domain, (START, END))
    _run(work, "metgrid", "Successful completion of metgrid")
    _metgrid_output(work, 2 if nested else 1)
    for table in (PREFIX / "share/wrf/run").iterdir():
        (work / table.name).symlink_to(table)
    (work / MYNN_HISTORY_FILE).write_text(MYNN_HISTORY_TEXT, "ascii")
    for exe in ("real", "wrf"):
        namelist = compile_wrf_run_namelist(domain, science, lock, source, for_real=exe == "real")
        if exe == "wrf":
            namelist["time_control"].update(run_hours=0, run_minutes=2)
        (work / "namelist.input").write_text(render_fortran_namelist(namelist), encoding="utf-8")
        marker = "SUCCESS COMPLETE REAL_EM INIT" if exe == "real" else "SUCCESS COMPLETE WRF"
        _run(work, exe, marker, mpi=True)
    _check_history(work, domain, science, 2 if nested else 1)


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve()
    root.mkdir(parents=True, exist_ok=False)
    selected = sys.argv[2] if len(sys.argv) > 2 else "both"
    if selected not in ("single", "nested", "both"):
        raise ValueError("Expected optional mode: single, nested, or both")
    for nested in (False, True) if selected == "both" else (selected == "nested",):
        work = root / ("nested" if nested else "single")
        work.mkdir()
        print(f"Synthetic fixture: {work}", flush=True)
        _fixture(work, nested)
    print("Synthetic native qualification complete; no forecast accuracy claim.")
