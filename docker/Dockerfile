# syntax=docker/dockerfile:1.7
FROM ghcr.io/astral-sh/uv:0.12.13 AS uv

FROM python:3.12-slim-bookworm AS python-base
WORKDIR /app
ENV PATH="/app/.venv/bin:${PATH}"
ENTRYPOINT ["av-wrf"]
CMD ["check"]

FROM python-base AS app
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    uv sync --locked --no-dev --no-install-project
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,target=/project \
    uv sync --project /project --locked --no-dev --no-editable
COPY config/ config/

FROM debian:bookworm-slim AS solver-build
RUN --mount=type=bind,source=docker/install-debian-packages.sh,target=/tmp/install-packages.sh \
    sh /tmp/install-packages.sh \
    autoconf automake bc build-essential ca-certificates csh file gfortran git \
    libjpeg62-turbo-dev libnetcdf-dev libnetcdff-dev libopenmpi-dev libpng-dev \
    libtool m4 openmpi-bin perl python3 time zlib1g-dev

ENV NETCDF=/opt/netcdf WRFIO_NCD_LARGE_FILE_SUPPORT=1 \
    JASPERLIB=/opt/jasper/lib JASPERINC=/opt/jasper/include
WORKDIR /build
ARG JOBS=8
ARG JASPER_TAG=version-1.900.29
RUN --mount=type=bind,source=docker/build-jasper.sh,target=/tmp/build-jasper.sh \
    bash /tmp/build-jasper.sh "${JASPER_TAG}" "${JOBS}"

COPY docker/patches/ patches/
ARG WRF_TAG=v4.8.0
RUN --mount=type=bind,source=docker/build-wrf.sh,target=/tmp/build-wrf.sh \
    bash /tmp/build-wrf.sh "${WRF_TAG}" "${JOBS}"

ARG WPS_TAG=v4.7.0
RUN --mount=type=bind,source=docker/build-wps.sh,target=/tmp/build-wps.sh \
    bash /tmp/build-wps.sh "${WPS_TAG}"
RUN --mount=type=bind,source=docker/package-solver.sh,target=/tmp/package-solver.sh \
    --mount=type=bind,source=docker/write-build-manifest.py,target=/tmp/write-build-manifest.py \
    bash /tmp/package-solver.sh

FROM python-base AS solver
RUN --mount=type=bind,source=docker/install-debian-packages.sh,target=/tmp/install-packages.sh \
    sh /tmp/install-packages.sh \
    libeccodes-tools libgfortran5 libjpeg62-turbo libnetcdff7 libpng16-16 openmpi-bin
COPY --from=solver-build /opt/av-wrf /opt/av-wrf
RUN --mount=type=bind,source=docker/check-solver.sh,target=/tmp/check-solver.sh \
    sh /tmp/check-solver.sh
COPY --from=app /app/ /app/

FROM app AS runtime
