#!/usr/bin/env bash
set -euo pipefail

prefix=/opt/av-wrf
wrf_run=$prefix/share/wrf/run
wps_tables=$prefix/share/wps
mkdir -p "$prefix/bin" "$wrf_run" "$wps_tables/Variable_Tables"
install -m 755 WRF/main/{real,wrf}.exe WPS/geogrid/src/geogrid.exe \
    WPS/metgrid/src/metgrid.exe WPS/ungrib/src/ungrib.exe "$prefix/bin/"
# Follow WRF's data symlinks while excluding examples and compiled programs.
find -L WRF/run -maxdepth 1 -type f ! -name '*.exe' ! -name 'namelist*' ! -name 'README*' \
    -exec cp -L -t "$wrf_run" -- {} +
find WRF/phys/noahmp/parameters -maxdepth 1 -type f -name '*.TBL' \
    -exec cp -t "$wrf_run" -- {} +
for table in qr_acr_qg_V4.dat qr_acr_qsV2.dat freezeH2O.dat; do
    test -s "thompson/$table"
    install -m 444 "thompson/$table" "$wrf_run/$table"
done
cp -L WPS/geogrid/GEOGRID.TBL.ARW WPS/metgrid/METGRID.TBL.ARW "$wps_tables/"
cp -L WPS/ungrib/Variable_Tables/Vtable.* "$wps_tables/Variable_Tables/"
for table in RRTMG_LW_DATA RRTMG_SW_DATA LANDUSE.TBL NoahmpTable.TBL; do
    test -s "$wrf_run/$table"
done

python3 "$(dirname "$0")/write-build-manifest.py" "$prefix/build-manifest.json"
