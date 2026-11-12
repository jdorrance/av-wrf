#!/usr/bin/env bash
set -euo pipefail

for patch in patches/wrf-*.patch; do
    if [[ -f "$patch" ]]; then git -C WRF apply "/build/$patch"; fi
done
cd WRF

# WRF's driver exits 1 after the menu is deliberately cancelled with -1.
menu=$(printf '%s\n' -1 | ./configure 2>&1 || true)
option=$(awk '/gfortran/ {
    for (i = 2; i <= NF; i++) if ($i == "(dmpar)") {
        sub(/\.$/, "", $(i-1)); print $(i-1); exit
    }
}' <<< "$menu")
[[ "$option" =~ ^[0-9]+$ ]] || { printf '%s\n' "$menu" >&2; exit 1; }
printf '%s\n1\n' "$option" | ./configure
sed -i -E 's/ -(march|mcpu)=native//g; s/ -f90=\$\(SFC\)//; s/ -cc=\$\(SCC\)//' configure.wrf
# WRF 4.8's classic Makefile omits the module order for the new Shin-Hong PBL.
printf '\n%s\n' 'physics_mmm/bl_shinhong.o: ccpp_kind_types.o' \
    'module_bl_shinhong.o: physics_mmm/bl_shinhong.o' >> phys/Makefile
./compile -j "$1" em_real
test -x main/wrf.exe && test -x main/real.exe
