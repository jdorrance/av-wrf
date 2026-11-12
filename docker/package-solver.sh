#!/usr/bin/env bash
set -euo pipefail

prefix=/opt/av-wrf
wrf_run=$prefix/share/wrf/run
wps_tables=$prefix/share/wps
mkdir -p "$prefix/bin" "$prefix/lib" "$wrf_run" "$wps_tables/Variable_Tables"
install -m 755 WRF/main/{real,wrf}.exe WPS/geogrid/src/geogrid.exe \
    WPS/metgrid/src/metgrid.exe WPS/ungrib/src/ungrib.exe "$prefix/bin/"
cp -P /opt/jasper/lib/libjasper.so.* "$prefix/lib/"

# Follow WRF's data symlinks while excluding examples and compiled programs.
find -L WRF/run -maxdepth 1 -type f ! -name '*.exe' ! -name 'namelist*' ! -name 'README*' \
    -exec cp -L -t "$wrf_run" -- {} +
find WRF/phys/noahmp/parameters -maxdepth 1 -type f -name '*.TBL' \
    -exec cp -t "$wrf_run" -- {} +
cp -L WPS/geogrid/GEOGRID.TBL.ARW WPS/metgrid/METGRID.TBL.ARW "$wps_tables/"
cp -L WPS/ungrib/Variable_Tables/Vtable.* "$wps_tables/Variable_Tables/"
for table in RRTMG_LW_DATA RRTMG_SW_DATA LANDUSE.TBL NoahmpTable.TBL; do
    test -s "$wrf_run/$table"
done

python3 - <<'PY'
import hashlib
import json
import pathlib
import subprocess

def version(*args):
    return subprocess.check_output(args, text=True).splitlines()[0].strip()

manifest = {
    "wrf_sha": version("git", "-C", "WRF", "rev-parse", "HEAD"),
    "wps_sha": version("git", "-C", "WPS", "rev-parse", "HEAD"),
    "patch_sha256s": [
        [patch.name, hashlib.sha256(patch.read_bytes()).hexdigest()]
        for patch in sorted(pathlib.Path("patches").glob("*.patch"))
    ],
    "compiler": "gfortran-" + version("gfortran", "-dumpfullversion"),
    "mpi": version("mpirun", "--version"),
}
pathlib.Path("/opt/av-wrf/build-manifest.json").write_text(
    json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY
