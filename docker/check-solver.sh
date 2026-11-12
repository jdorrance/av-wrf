#!/bin/sh
set -eu

prefix=/opt/av-wrf
printf '%s\n' "$prefix/lib" > /etc/ld.so.conf.d/av-wrf.conf
ldconfig

for program in real wrf geogrid metgrid ungrib; do
    exe=$prefix/bin/$program.exe
    if [ ! -f "$exe" ] || [ ! -x "$exe" ]; then
        printf 'Missing solver executable: %s\n' "$exe" >&2
        exit 1
    fi
    links=$(ldd "$exe")
    case $links in
        *'not found'*) printf '%s: unresolved libraries\n%s\n' "$exe" "$links" >&2; exit 1 ;;
    esac
    if [ "$program" = ungrib ]; then
        if ! printf '%s\n' "$links" | grep -F "$prefix/lib/libjasper.so"; then
            printf '%s: packaged JasPer library is missing\n' "$exe" >&2
            exit 1
        fi
    fi
done

grib_ls -V
