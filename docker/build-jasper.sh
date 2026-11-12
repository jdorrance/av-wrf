#!/usr/bin/env bash
set -euo pipefail

git clone --depth 1 --branch "$1" https://github.com/jasper-software/jasper.git jasper
cd jasper
autoreconf -i
CFLAGS="-O2 -fPIC" ./configure --prefix=/opt/jasper --enable-shared --disable-opengl
make -j "$2"
make install
