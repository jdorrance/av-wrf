#!/usr/bin/env bash
set -euxo pipefail

trap 'status=$?; echo "prebake-thompson failed at line $LINENO (status $status)" >&2;
      ls -l "$PWD" >&2 || true; exit $status' ERR

# WRF v4.8 module_mp_thompson.F creates these in wrf_init, not ideal.exe.
wrf=$PWD
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
chmod 755 "$scratch"
mkdir "$scratch/generate" "$scratch/read"
for work in "$scratch/generate" "$scratch/read"; do
    for file in "$wrf"/run/*; do
        case "${file##*/}" in input_sounding|namelist*|*.exe|README*) continue ;; esac
        [[ -f "$file" ]] && ln -s "$file" "$work/${file##*/}"
    done
    cp "$wrf/test/em_quarter_ss/input_sounding" "$work/"
    sed -E \
        -e 's/^( run_minutes[[:space:]]*=).*/\1 0,/' \
        -e 's/^( run_seconds[[:space:]]*=).*/\1 12,/' \
        -e 's/^( end_minute[[:space:]]*=).*/\1 00, 00, 00,/' \
        -e 's/^( end_second[[:space:]]*=).*/\1 12, 12, 12,/' \
        -e 's/^( mp_physics[[:space:]]*=).*/\1 8, 8, 8,/' \
        -e '/^ &physics/a\ force_read_thompson = .false.,\n write_thompson_tables = .true.,' \
        "$wrf/test/em_quarter_ss/namelist.input" > "$work/namelist.input"
done

export OMPI_ALLOW_RUN_AS_ROOT=1 OMPI_ALLOW_RUN_AS_ROOT_CONFIRM=1 OMP_NUM_THREADS=1
cd "$scratch/generate"
if ! timeout 120 mpirun -np 1 "$wrf/main/ideal.exe" > ideal.log 2>&1; then
    cat ideal.log rsl.error.0000 >&2; exit 1
fi
test -s wrfinput_d01
if ! timeout 1800 mpirun -np 1 "$wrf/main/wrf.exe" > wrf.log 2>&1; then
    cat wrf.log rsl.error.0000 >&2; exit 1
fi
grep -q 'SUCCESS COMPLETE WRF' rsl.error.0000
mkdir -p /build/thompson
for table in qr_acr_qg_V4.dat qr_acr_qsV2.dat freezeH2O.dat; do
    test -s "$table"
    install -m 444 "$table" "/build/thompson/$table"
    ln -s "/build/thompson/$table" "$scratch/read/$table"
done
sha256sum /build/thompson/*.dat > "$scratch/checksums"
cp wrfinput_d01 "$scratch/read/"
cd "$scratch/read"
# A missing or unreadable table must fail rather than silently recompute it.
sed -i -e 's/force_read_thompson = .false./force_read_thompson = .true./' \
    -e 's/write_thompson_tables = .true./write_thompson_tables = .false./' namelist.input
chown -R nobody:nogroup "$scratch/read"
if ! runuser -u nobody -- timeout 300 mpirun -np 1 "$wrf/main/wrf.exe" > wrf.log 2>&1; then
    cat wrf.log rsl.error.0000 >&2; exit 1
fi
grep -q 'SUCCESS COMPLETE WRF' rsl.error.0000
for table in qr_acr_qg_V4.dat qr_acr_qsV2.dat freezeH2O.dat; do
    # Fortran pads the fixed-width graupel table name before printing the status.
    grep -Eq "ThompMP: read ${table/./[.]}[[:space:]]+instead of computing" wrf.log rsl.*
done
sha256sum -c "$scratch/checksums"
