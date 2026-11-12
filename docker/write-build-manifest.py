"""Record solver source commits, patches, compiler, and MPI."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path


def _version(*args: str) -> str:
    return subprocess.check_output(args, text=True).splitlines()[0].strip()


if __name__ == "__main__":
    manifest = {
        **{
            f"{repo.lower()}_sha": _version("git", "-C", repo, "rev-parse", "HEAD")
            for repo in ("WRF", "WPS", "jasper")
        },
        "patch_sha256s": [
            [patch.name, hashlib.sha256(patch.read_bytes()).hexdigest()]
            for patch in sorted(
                path
                for project in ("wrf", "wps")
                for path in Path("patches").glob(f"{project}-*.patch")
                if path.is_file()
            )
        ],
        "compiler": "gfortran-" + _version("gfortran", "-dumpfullversion"),
        "mpi": _version("mpirun", "--version"),
    }
    Path(sys.argv[1]).write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", "utf-8")
