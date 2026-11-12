# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM ghcr.io/astral-sh/uv:0.12.13@sha256:b485bd65cc2cf1c9a93b3554012c9c3778cf7b1b5fd3d3096ce9e1226c97e1e6 AS uv

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df AS python-base
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
    uv sync --project /project --locked --no-dev --no-editable --reinstall-package av-wrf
COPY config/ config/

FROM app AS solver-app
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,target=/project \
    uv sync --project /project --locked --no-dev --no-editable --extra icon

FROM debian:bookworm-slim@sha256:734a72b3d6142a71993b4bc580ca01d8ca687f77a2c422d96a961d3376b179bc AS solver-build
RUN --mount=type=bind,source=docker/install-debian-packages.sh,target=/tmp/install-packages.sh \
    sh /tmp/install-packages.sh \
    bc build-essential ca-certificates csh file gfortran git \
    libnetcdf-dev libnetcdff-dev libopenmpi-dev openmpi-bin perl python3 time

ENV NETCDF=/opt/netcdf WRFIO_NCD_LARGE_FILE_SUPPORT=1
WORKDIR /build
ARG JOBS=8
ARG WRF_PORTABLE_ISA=baseline

COPY docker/patches/wrf-*.patch patches/
RUN --mount=type=bind,source=docker/build-wrf.sh,target=/tmp/build-wrf.sh \
    WRF_PORTABLE_ISA="${WRF_PORTABLE_ISA}" bash /tmp/build-wrf.sh "${JOBS}"
# Keep native compilation cached when the table-generation qualification needs adjustment.
RUN --mount=type=bind,source=docker/prebake-thompson.sh,target=/tmp/prebake-thompson.sh \
    bash -c 'cd /build/WRF && bash /tmp/prebake-thompson.sh'

COPY docker/patches/wps-*.patch patches/
RUN --mount=type=bind,source=docker/build-wps.sh,target=/tmp/build-wps.sh \
    bash /tmp/build-wps.sh
RUN --mount=type=bind,source=docker/package-solver.sh,target=/tmp/package-solver.sh \
    --mount=type=bind,source=docker/write-build-manifest.py,target=/tmp/write-build-manifest.py \
    bash /tmp/package-solver.sh

FROM python-base AS solver
RUN --mount=type=bind,source=docker/install-debian-packages.sh,target=/tmp/install-packages.sh \
    sh /tmp/install-packages.sh \
    libeccodes-tools libgfortran5 libnetcdff7 openmpi-bin
COPY --from=solver-build /opt/av-wrf /opt/av-wrf
RUN --mount=type=bind,source=docker/check-solver.sh,target=/tmp/check-solver.sh \
    sh /tmp/check-solver.sh
COPY --from=solver-app /app/ /app/

FROM app AS runtime
