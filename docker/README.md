# Container Build (`docker/`)

Multi-stage `Dockerfile` providing two targets on `linux/arm64` (native on Apple
Silicon via OrbStack or Docker Desktop) with zero region IDs baked into the image:

1. **`runtime`** (final stage, default `docker build .`): lean Python 3.12
   producer (`check`, `plan`, `prep`, `post`, `publish`, `reconcile`).
2. **`solver`** (`--target solver`): compiles pinned
   [Jasper 1.900.29](https://github.com/jasper-software/jasper),
   [WRF v4.8.0](https://github.com/wrf-model/WRF/releases) (`-DUSE_MPI=ON`,
   `ARW` `EM_REAL`), and [WPS v4.7.0](https://github.com/wrf-model/WPS/releases)
   (`-DWRF_DIR=/opt/av-wrf`, `-DUSE_MPI=ON`) via CMake + Ninja, verifies all
   five binaries (`wrf.exe`, `real.exe`, `geogrid.exe`, `metgrid.exe`,
   `ungrib.exe`) and shared libraries via `ldd`, and writes
   `/opt/av-wrf/build-manifest.json` including SHA-256 digests of any
   `docker/patches/*.patch` files.

```sh
# Fast default producer image:
docker buildx build --platform linux/arm64 -f docker/Dockerfile -t av-wrf:runtime .

# Full MPI WRF v4.8.0 + WPS v4.7.0 solver image (smoke check runs `av-wrf check`):
docker buildx build --platform linux/arm64 --target solver --build-arg JOBS=12 -f docker/Dockerfile -t av-wrf:solver .
docker run --rm av-wrf:solver
```

Mount static WPS geography at `/opt/av-wrf/geog` (`geog_data_path` in domain
profiles). Solver patches in [`patches/`](patches/README.md) are locked in
[`manifest.lock`](../manifest.lock).
