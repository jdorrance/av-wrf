#!/bin/sh
set -eu

prefix=/opt/av-wrf
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
done

grib_ls -V
