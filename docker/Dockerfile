# syntax=docker/dockerfile:1.7
FROM python:3.12-slim-bookworm AS runtime

ARG DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates libhdf5-dev libnetcdf-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml LICENSE README.md ./
COPY src/ ./src/
COPY config/ ./config/
COPY vendor/ ./vendor/

RUN pip install --no-cache-dir --no-compile .

# Phase 2 adds a multi-stage solver layer compiling pinned WRF v4.8.0 + WPS v4.7.0
# into /opt/av-wrf/bin and writing /opt/av-wrf/build-manifest.json.
ENTRYPOINT ["av-wrf"]
CMD ["check", "--product", "wrf-armenia-3km"]
