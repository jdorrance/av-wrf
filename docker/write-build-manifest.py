"""Record the WRF, WPS, patches, compiler, and MPI used to build the solver image."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def _version(*args: str) -> str:
    return subprocess.check_output(args, text=True).splitlines()[0].strip()


if __name__ == "__main__":
    manifest = {
        "wrf_sha": _version("git", "-C", "WRF", "rev-parse", "HEAD"),
        "wps_sha": _version("git", "-C", "WPS", "rev-parse", "HEAD"),
        "patch_sha256s": [
            [patch.name, hashlib.sha256(patch.read_bytes()).hexdigest()]
            for patch in sorted(Path("patches").glob("*.patch"))
        ],
        "compiler": "gfortran-" + _version("gfortran", "-dumpfullversion"),
        "mpi": _version("mpirun", "--version"),
    }
    Path(sys.argv[1]).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", "utf-8")
