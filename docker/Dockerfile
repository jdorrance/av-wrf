# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS runtime-base
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libzstd1 \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}" UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN --mount=from=ghcr.io/astral-sh/uv:0.12.13,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --link-mode=copy
COPY LICENSE README.md ./
COPY src/ src/
COPY scripts/ scripts/
RUN --mount=from=ghcr.io/astral-sh/uv:0.12.13,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --link-mode=copy
COPY config/ config/
ENTRYPOINT ["av-wrf"]
CMD ["check"]

FROM debian:bookworm-slim AS solver-build
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG JOBS=8
ARG WRF_TAG=v4.8.0
ARG JASPER_TAG=version-1.900.29
RUN apt-get update && apt-get install -y --no-install-recommends \
    autoconf automake bc build-essential ca-certificates csh file gfortran git \
    libjpeg62-turbo-dev libnetcdf-dev libnetcdff-dev libopenmpi-dev libpng-dev \
    libtool m4 openmpi-bin perl python3 time zlib1g-dev \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /opt/netcdf/lib \
    && ln -s /usr/include /usr/bin /opt/netcdf/ \
    && ln -s "/usr/lib/$(dpkg-architecture -qDEB_HOST_MULTIARCH)"/libnetcdf* /opt/netcdf/lib/

ENV NETCDF=/opt/netcdf WRFIO_NCD_LARGE_FILE_SUPPORT=1 \
    JASPERLIB=/opt/jasper/lib JASPERINC=/opt/jasper/include
WORKDIR /build
RUN git clone --depth 1 --branch "${JASPER_TAG}" https://github.com/jasper-software/jasper.git \
    && cd jasper && autoreconf -i \
    && CFLAGS="-O2 -fPIC" ./configure --prefix=/opt/jasper --enable-shared --disable-opengl \
    && make -j "${JOBS}" && make install

RUN git clone --depth 1 --branch "${WRF_TAG}" https://github.com/wrf-model/WRF.git \
    && git -C WRF submodule update --init --depth 1 -- \
       phys/noahmp phys/physics_mmm phys/MYNN-EDMF phys/MYNN-SFC phys/TEMPO phys/GFL
COPY docker/patches/ patches/
COPY docker/build-wrf.sh ./
RUN bash build-wrf.sh "${JOBS}"

ARG WPS_TAG=v4.7.0
RUN git clone --depth 1 --branch "${WPS_TAG}" https://github.com/wrf-model/WPS.git
COPY docker/build-wps.sh ./
RUN bash build-wps.sh
COPY docker/package-solver.sh ./
RUN bash package-solver.sh

FROM runtime-base AS solver
RUN apt-get update && apt-get install -y --no-install-recommends \
    libeccodes-tools libgfortran5 libjpeg62-turbo libnetcdff7 libpng16-16 openmpi-bin \
    && rm -rf /var/lib/apt/lists/*
COPY --from=solver-build /opt/av-wrf /opt/av-wrf
RUN printf '%s\n' /opt/av-wrf/lib > /etc/ld.so.conf.d/av-wrf.conf && ldconfig \
    && for exe in /opt/av-wrf/bin/*.exe; do \
      links="$(ldd "$exe")" || exit 1; \
      if printf '%s\n' "$links" | grep -q 'not found'; then printf '%s\n' "$links"; exit 1; fi; \
    done \
    && ldd /opt/av-wrf/bin/ungrib.exe | grep libjasper \
    && grib_ls -V

FROM runtime-base AS runtime
