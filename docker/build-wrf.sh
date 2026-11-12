#!/usr/bin/env bash
set -euo pipefail

jobs=${1:?Build job count is required}

# The classic build expects NetCDF headers, tools and libraries under one prefix.
multiarch=$(dpkg-architecture -qDEB_HOST_MULTIARCH)
mkdir -p "$NETCDF/lib"
ln -s /usr/include /usr/bin "$NETCDF/"
ln -s "/usr/lib/$multiarch"/libnetcdf* "$NETCDF/lib/"

git clone --depth 1 --branch v4.8.0 https://github.com/wrf-model/WRF.git
test "$(git -C WRF rev-parse HEAD)" = 06d4240ae989cc3e50af412bb472df3d9048783c
git -C WRF submodule update --init --depth 1 -- \
    phys/noahmp phys/physics_mmm phys/MYNN-EDMF phys/MYNN-SFC phys/TEMPO phys/GFL
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
# Targeted binaries require compatible CPUs at build time and deployment time.
case "${WRF_PORTABLE_ISA:-baseline}:$(uname -m)" in
    baseline:x86_64|baseline:aarch64|baseline:arm64) isa_flags="" ;;
    x86-64-v3:x86_64) isa_flags="-march=x86-64-v3" ;;
    armv8.2-a:aarch64|armv8.2-a:arm64) isa_flags="-march=armv8.2-a+fp16+dotprod" ;;
    *) printf 'Unsupported WRF_PORTABLE_ISA or architecture: %s on %s\n' \
        "${WRF_PORTABLE_ISA:-baseline}" "$(uname -m)" >&2; exit 1 ;;
esac
if [[ -n "$isa_flags" ]]; then
    sed -i -E "s/^FCOPTIM(.*)/FCOPTIM\1 -O3 -ftree-vectorize $isa_flags/" configure.wrf
fi
# WRF 4.8's classic Makefile omits the module order for the new Shin-Hong PBL.
printf '\n%s\n' 'physics_mmm/bl_shinhong.o: ccpp_kind_types.o' \
    'module_bl_shinhong.o: physics_mmm/bl_shinhong.o' >> phys/Makefile
./compile -j "$jobs" em_quarter_ss
test -x main/wrf.exe && test -x main/ideal.exe
./compile -j "$jobs" em_real
test -x main/wrf.exe && test -x main/real.exe
