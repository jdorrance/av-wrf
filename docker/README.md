# Container build

The default image runs the Python producer. The `solver` target adds Linux
WRF-ARW, WPS, Open MPI, ecCodes, and the Python ICON acquisition dependencies.

```sh
docker buildx build --load -f docker/Dockerfile -t av-wrf:runtime .
docker buildx build --load --platform linux/arm64 --target solver \
  -f docker/Dockerfile -t av-wrf:solver .
docker run --rm av-wrf:solver check
```

Both targets ship the installed app and config. BuildKit mounts the sources
for `uv sync` and caches dependencies from `uv.lock`. The app layer rebuilds
the local wheel when the build context changes. The solver caches its native
libraries and link checks before copying the app.
[Astral Docker guide](https://docs.astral.sh/uv/guides/integration/docker/)

BuildKit mounts each script only for its layer. [`build-wrf.sh`](build-wrf.sh) and
[`build-wps.sh`](build-wps.sh) check out and compile the native solvers.
[`package-solver.sh`](package-solver.sh) collects their runtime files and
writes the manifest. [`check-solver.sh`](check-solver.sh) checks the packaged
executables and libraries in the final image.
[WPS CMake ungrib issue](https://github.com/wrf-model/WPS/issues/279)

Mount WPS geography at the configured `geog_data_path`. Run `geog`, pass its
`geo_em.d01.nc` to `plan` and `solve`, and use `forcing-manifest` to seal the
local GRIB2 files after acquisition. Set `AV_WRF_OCI_DIGEST` to the solver
image digest for both `plan` and `solve`. Set `AV_WRF_STORE_URI` to a mounted
store for live runs. See [solver patches](patches/README.md) for build provenance.

The [image workflow](../.github/workflows/images.yml) checks ICON normalization
and ungrib, then runs both WRF executables through the first input open.
These checks run offline as an unprivileged user.
Set `--user` to the UID and GID with write access to your mounted scratch and store.

Set `AV_WRF_MPI_LAUNCHER='srun --mpi=pmix -n'` to use a compatible Slurm
installation. The runner appends the locked rank count and solver executable.
