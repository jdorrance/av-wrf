"""Check generated WRF namelists with both native executables in an empty directory."""

import re
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from av_wrf.contracts import BuildManifest, ScienceProfile
from av_wrf.core import (
    MYNN_HISTORY_FILE,
    MYNN_HISTORY_TEXT,
    compile_wrf_run_namelist,
    render_fortran_namelist,
)
from av_wrf.runner import LocalFileStore, check_all_products, load_bundle, plan_product_cycle


def _check_resolved_options(work: Path, science: ScienceProfile, max_dom: int) -> None:
    output = (work / "namelist.output").read_text("ascii")
    # Fortran leaves omitted array entries at Registry defaults; inspect every active domain.
    # https://github.com/wrf-model/WRF/blob/v4.8.0/Registry/Registry.EM_COMMON
    for name, value in (
        ("shinhong_scu_mixing", science.shinhong_scu_mixing),
        ("shinhong_nonlocal_flux", science.shinhong_nonlocal_flux),
        ("gwd_opt", science.gwd_opt),
        ("gwd_if_nonhyd", science.gwd_if_nonhyd),
    ):
        match = re.search(
            rf"(?ms)^\s*{name.upper()}\s*=\s*(.*?)(?=^\s*[A-Z][A-Z0-9_]*\s*=|^\s*/)",
            output,
        )
        if match is None:
            raise ValueError(f"Missing native {name} in {work}")
        actual: list[str] = []
        for token in match[1].split(","):
            if not token.strip():
                continue
            item = re.fullmatch(r"(?:(\d+)\*)?([TF]|-?\d+)", token.strip())
            if item is None:
                raise ValueError(f"Unexpected native {name} value {token!r}")
            actual.extend([item[2]] * int(item[1] or "1"))
        expected = ("T" if value else "F") if isinstance(value, bool) else str(value)
        if actual[:max_dom] != [expected] * max_dom:
            raise ValueError(f"Native {name}={actual[:max_dom]} differs from {expected} in {work}")


def _check_native_namelists(config: Path, product_id: str) -> tuple[int, bool]:
    bundle = load_bundle(config, product_id)
    origin = f"2026-01-15T{bundle.product.publish_origin_hour_utc:02d}:00:00Z"
    with TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        _, lock = plan_product_cycle(
            config,
            LocalFileStore(scratch / "store"),
            product_id=product_id,
            publish_origin_utc=origin,
            attempt_id="native-parser",
            mpi_ranks=1,
            build_manifest=BuildManifest("host-dev-unpinned", "smoke", "smoke", (), "", ""),
            bundle=bundle,
        )
        qualified_physics = True
        for exe, input_error in (
            ("real", "error opening met_em.d01."),
            ("wrf", "error opening wrfinput_d01 for reading"),
        ):
            work = scratch / exe
            work.mkdir()
            sections = compile_wrf_run_namelist(
                bundle.domain, bundle.science, lock, bundle.primary_source, for_real=exe == "real"
            )
            max_dom = sections["domains"]["max_dom"]
            if not isinstance(max_dom, int):
                raise ValueError("max_dom must be scalar")
            qualified_physics &= (
                sections["physics"]["bl_pbl_physics"] == (5 if max_dom == 1 else (5,) * max_dom)
                and sections["physics"]["sf_lake_physics"]
                == (1 if max_dom == 1 else (1,) * max_dom)
                and sections["domains"].get("use_adaptive_time_step") is True
            )
            (work / "namelist.input").write_text(render_fortran_namelist(sections), "utf-8")
            if "iofields_filename" in sections["time_control"]:
                (work / MYNN_HISTORY_FILE).write_text(MYNN_HISTORY_TEXT, "ascii")
            proc = subprocess.run(
                ["mpirun", "-np", "1", f"/opt/av-wrf/bin/{exe}.exe"],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            log_path = work / "rsl.error.0000"
            log = log_path.read_text("utf-8", errors="replace") if log_path.is_file() else ""
            if proc.returncode == 0 or input_error not in log:
                raise RuntimeError(f"{exe}.exe failed to reach input opening\n{log}{proc.stderr}")
            _check_resolved_options(work, bundle.science, max_dom)
            print(f"{exe}.exe read {product_id} ({max_dom} domains) and reached input opening")
        return max_dom, qualified_physics


if __name__ == "__main__":
    config = Path("config")
    coverage = {
        _check_native_namelists(config, product.product_id)
        for product in check_all_products(config)
    }
    if not {(1, True), (2, True)} <= coverage:
        raise RuntimeError(f"Configured MYNN/lake/adaptive parser coverage incomplete: {coverage}")
