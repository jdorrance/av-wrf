# Container build

The default image runs the Python producer. The `solver` target adds Linux
WRF-ARW, WPS, Open MPI and ecCodes.

```sh
docker buildx build --load -f docker/Dockerfile -t av-wrf:runtime .
docker buildx build --load --platform linux/arm64 --target solver \
  -f docker/Dockerfile -t av-wrf:solver .
docker run --rm av-wrf:solver check
```

Both targets install Python dependencies from `uv.lock`. The solver uses
[WRF](https://github.com/wrf-model/WRF/releases) and
[WPS](https://github.com/wrf-model/WPS/releases) with their classic GNU builds.
The WPS classic build keeps GRIB2 decoding on the tested path.
[WPS ungrib build issue](https://github.com/wrf-model/WPS/issues/279)

[`build-wrf.sh`](build-wrf.sh) and [`build-wps.sh`](build-wps.sh) compile in separate layers.
[`package-solver.sh`](package-solver.sh) installs the five runtime executables,
their data tables, the JasPer shared library and a build manifest. The final
solver stage verifies dynamic linkage.

Mount WPS geography at the configured `geog_data_path`. Run `geog`, pass its
`geo_em.d01.nc` to `plan` and `solve`, and use `forcing-manifest` to seal the
local GRIB2 files after acquisition. Set `AV_WRF_OCI_DIGEST` to the solver
image digest for both `plan` and `solve`. Set `AV_WRF_STORE_URI` to a mounted
durable store for live runs. Solver patches in
[patches/](patches/README.md) are locked in [manifest.lock](../manifest.lock).

Set `AV_WRF_MPI_LAUNCHER='srun --mpi=pmix -n'` to use a compatible Slurm
installation. The runner appends the locked rank count and solver executable.
