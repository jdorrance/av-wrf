#!/usr/bin/env bash
set -euo pipefail

for patch in patches/wps-*.patch; do
    if [[ -f "$patch" ]]; then git -C WPS apply "/build/$patch"; fi
done
cd WPS

# WPS 4.7 classic configure covers GNU on x86_64. The same MPI/GRIB2 stanza works on arm64.
if [[ $(uname -m) == aarch64 ]]; then
    sed -i -E 's/(Linux[[:space:]]+x86_64), gfortran/\1 aarch64, gfortran/' \
        arch/configure.defaults
fi
export WRF_DIR=/build/WRF
menu=$(printf '%s\n' -1 | ./configure 2>&1 || true)
option=$(awk '/\(dmpar\)/ && /gfortran/ && !/NO_GRIB2/ { sub(/\.$/, "", $1); print $1; exit }' <<< "$menu")
[[ "$option" =~ ^[0-9]+$ ]] || { printf '%s\n' "$menu" >&2; exit 1; }
printf '%s\n' "$option" | ./configure
sed -i -E \
    -e '/^(FFLAGS|F77FLAGS)[[:space:]]*=/ s/$/ -fno-stack-arrays -fallow-argument-mismatch -fallow-invalid-boz/' \
    -e 's/-ljasper/-ljasper -ljpeg/g' configure.wps
./compile
test -x geogrid/src/geogrid.exe
test -x metgrid/src/metgrid.exe
test -x ungrib/src/ungrib.exe
