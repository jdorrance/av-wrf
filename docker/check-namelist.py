"""Check generated WRF namelists with both native executables in an empty directory."""

import math
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from av_wrf.contracts import BuildManifest
from av_wrf.core import compile_wrf_run_namelist, render_fortran_namelist
from av_wrf.runner import LocalFileStore, check_all_products, load_bundle, plan_product_cycle


def _check_native_namelists() -> None:
    config = Path("config")
    product = min(check_all_products(config), key=lambda item: math.prod(item.published_yx))
    bundle = load_bundle(config, product.product_id)
    origin = f"2026-01-15T{bundle.product.publish_origin_hour_utc:02d}:00:00Z"
    with TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        _, lock = plan_product_cycle(
            config,
            LocalFileStore(scratch / "store"),
            product_id=product.product_id,
            publish_origin_utc=origin,
            attempt_id="native-parser",
            mpi_ranks=1,
            build_manifest=BuildManifest("host-dev-unpinned", "smoke", "smoke", (), "", ""),
            bundle=bundle,
        )
        for exe, input_error in (
            ("real", "error opening met_em.d01."),
            ("wrf", "error opening wrfinput_d01 for reading"),
        ):
            work = scratch / exe
            work.mkdir()
            sections = compile_wrf_run_namelist(
                bundle.domain, bundle.science, lock, bundle.primary_source, for_real=exe == "real"
            )
            (work / "namelist.input").write_text(render_fortran_namelist(sections), "utf-8")
            proc = subprocess.run(
                ["mpirun", "--allow-run-as-root", "-np", "1", f"/opt/av-wrf/bin/{exe}.exe"],
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
            print(f"{exe}.exe accepted the generated namelist and reached input opening")


if __name__ == "__main__":
    _check_native_namelists()
