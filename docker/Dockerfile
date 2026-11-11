# syntax=docker/dockerfile:1.7

# Stage 1: Shared Python / NetCDF / Zarr v3 producer base
FROM python:3.12-slim-bookworm AS runtime-base

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libnetcdf19 libzstd1 && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml LICENSE README.md ./
COPY src/ ./src/
COPY config/ ./config/
COPY vendor/ ./vendor/

RUN pip install --no-cache-dir --no-compile .

# Stage 2: Pinned Linux arm64/amd64 CMake WRF v4.8.0 + WPS v4.7.0 + Jasper 1.900.29 builder
FROM debian:bookworm-slim AS solver-builder

ARG DEBIAN_FRONTEND=noninteractive
ARG WRF_TAG=v4.8.0
ARG WPS_TAG=v4.7.0
ARG JASPER_TAG=version-1.900.29
ARG JOBS=8

RUN apt-get update && apt-get install -y --no-install-recommends \
    autoconf automake build-essential ca-certificates cmake curl gfortran git \
    libjpeg62-turbo-dev libnetcdf-dev libnetcdff-dev libopenmpi-dev libpng-dev \
    libtool m4 ninja-build openmpi-bin pkg-config python3 zlib1g-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY docker/patches/ /build/patches/

RUN git clone --depth 1 --branch "${JASPER_TAG}" https://github.com/jasper-software/jasper.git && \
    cd /build/jasper && autoreconf -i && \
    CFLAGS="-O2 -fPIC" ./configure --prefix=/opt/av-wrf --enable-shared --disable-opengl && \
    make -j "${JOBS}" && make install

RUN git clone --depth 1 --branch "${WRF_TAG}" https://github.com/wrf-model/WRF.git && \
    git clone --depth 1 --branch "${WPS_TAG}" https://github.com/wrf-model/WPS.git && \
    for p in /build/patches/wrf-*.patch; do [ -f "$p" ] && git -C /build/WRF apply "$p" || true; done && \
    for p in /build/patches/wps-*.patch; do [ -f "$p" ] && git -C /build/WPS apply "$p" || true; done

# WRF v4.8 FindNetCDF searches <nf-config --prefix>/{lib,lib64} with NO_DEFAULT_PATH;
# symlink Debian multiarch libnetcdff onto /usr/lib before configure (arm64 & amd64).
# Jasper 1.900.29 is built for WPS ungrib GRIB2 decoding; wrf.exe reads metgrid NetCDF.
RUN multiarch="$(dpkg-architecture -qDEB_HOST_MULTIARCH)" && \
    ln -sf "/usr/lib/${multiarch}/libnetcdff.so" /usr/lib/libnetcdff.so && \
    cmake -B /build/wrf-build -S /build/WRF -G Ninja \
      -DCMAKE_INSTALL_PREFIX=/opt/av-wrf \
      -DCMAKE_PREFIX_PATH=/opt/av-wrf \
      -DCMAKE_BUILD_TYPE=Release \
      -DUSE_MPI=ON \
      -DUSE_JASPER=OFF \
      -DWRF_CORE=ARW \
      -DWRF_CASE=EM_REAL \
      -DWRF_NESTING=BASIC && \
    cmake --build /build/wrf-build -j "${JOBS}" && \
    cmake --install /build/wrf-build

RUN cmake -B /build/wps-build -S /build/WPS -G Ninja \
      -DCMAKE_INSTALL_PREFIX=/opt/av-wrf \
      -DCMAKE_PREFIX_PATH=/opt/av-wrf \
      -DWRF_DIR=/opt/av-wrf \
      -DUSE_MPI=ON \
      -DCMAKE_BUILD_TYPE=Release && \
    cmake --build /build/wps-build -j "${JOBS}" && \
    cmake --install /build/wps-build

RUN python3 - <<'PY'
import hashlib, json, pathlib, subprocess

wrf_sha = subprocess.check_output(["git", "-C", "/build/WRF", "rev-parse", "HEAD"], text=True).strip()
wps_sha = subprocess.check_output(["git", "-C", "/build/WPS", "rev-parse", "HEAD"], text=True).strip()
gfortran_ver = subprocess.check_output(["gfortran", "-dumpfullversion"], text=True).strip()
mpi_ver = subprocess.check_output(["mpirun", "--version"], text=True).splitlines()[0].strip()
patches = [
    [p.name, hashlib.sha256(p.read_bytes()).hexdigest()]
    for p in sorted(pathlib.Path("/build/patches").glob("*.patch"))
]
manifest = {
    "wrf_sha": wrf_sha,
    "wps_sha": wps_sha,
    "patch_sha256s": patches,
    "compiler": f"gfortran-{gfortran_ver}",
    "mpi": mpi_ver,
}
out = pathlib.Path("/opt/av-wrf/build-manifest.json")
out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
for exe in ("wrf.exe", "real.exe", "geogrid.exe", "metgrid.exe", "ungrib.exe"):
    matches = list(pathlib.Path("/opt/av-wrf").rglob(exe))
    if not matches:
        raise SystemExit(f"Missing compiled solver executable: {exe}")
PY

# Stage 3: Complete MPI solver + producer image (`docker buildx build --target solver`)
FROM runtime-base AS solver

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgfortran5 libjpeg62-turbo libnetcdff7 libopenmpi3 libpng16-16 \
    openmpi-bin && \
    rm -rf /var/lib/apt/lists/*

COPY --from=solver-builder /opt/av-wrf /opt/av-wrf
ENV PATH="/opt/av-wrf/bin:${PATH}" \
    LD_LIBRARY_PATH="/opt/av-wrf/lib"
RUN set -eux; \
    for b in wrf.exe real.exe geogrid.exe metgrid.exe ungrib.exe; do \
      exe_path="$(find /opt/av-wrf -name "$b" -type f | head -n 1)"; \
      test -x "${exe_path}"; \
      ! ldd "${exe_path}" | grep -q "not found"; \
    done
ENTRYPOINT ["av-wrf"]
CMD ["check"]

# Stage 4 (default target): Fast pure-Python / Zarr v3 producer image (`docker build .`)
FROM runtime-base AS runtime
ENTRYPOINT ["av-wrf"]
CMD ["check"]
