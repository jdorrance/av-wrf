#!/usr/bin/env bash
set -euo pipefail

git clone --depth 1 --branch version-1.900.29 https://github.com/jasper-software/jasper.git jasper
cd jasper
test "$(git rev-parse HEAD)" = d9d95a0504bb0cd2857029ef3c5968762294b614
autoreconf -i
CFLAGS="-O2 -fPIC" ./configure --prefix=/opt/jasper --enable-shared --disable-opengl
make -j "${1:?Build job count is required}"
make install
