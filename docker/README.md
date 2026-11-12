# Container build

The default image runs the Python producer. The `solver` target adds Linux
WRF-ARW, WPS, Open MPI, ecCodes, and the Python ICON acquisition dependencies.

```sh
docker buildx build --load -f docker/Dockerfile -t av-wrf:runtime .
docker buildx build --load --platform linux/arm64 --target solver \
  -f docker/Dockerfile -t av-wrf:solver .
docker run --rm av-wrf:solver check
```

Both targets ship the app and config. Base images and CI actions use digest/SHA
pins. Refresh them explicitly and rerun native checks before promoting an image.
Debian packages resolve at build time; deployment locks the resulting OCI digest.

The solver defaults to the portable compiler baseline. Opt into a matching CPU
target with `--build-arg WRF_PORTABLE_ISA=x86-64-v3` on amd64 or
`--build-arg WRF_PORTABLE_ISA=armv8.2-a` on arm64. The latter requires FP16 and
dot-product extensions. Both the build machine and every runtime CPU must
support the selected target; incompatible architecture selections fail the build.

[`prebake-thompson.sh`](prebake-thompson.sh) runs the pinned ideal case for one
timestep, then reloads its Thompson tables as an unprivileged user with forced
read-only table access. [`package-solver.sh`](package-solver.sh) packages these
tables, runtime files, and the build manifest.

Mount [WPS geography](https://www2.mmm.ucar.edu/wrf/site/access_code/geog_data.html)
at the configured `geog_data_path`, including lake depth for the lake physics
profile. Run `geog`, pass its
output to `plan` and `solve`, and seal the local forcing with
`forcing-manifest`. Set `AV_WRF_OCI_DIGEST` to the solver
image digest for both `plan` and `solve`. Set `AV_WRF_STORE_URI` to a mounted
store for live runs. See [solver patches](patches/README.md) for build provenance.

The [image workflow](../.github/workflows/images.yml) keeps separate runtime and
solver [BuildKit caches](https://docs.docker.com/build/ci/github-actions/cache/)
and cancels superseded PR jobs. Native checks cover ICON/ungrib, resolved
namelists, and single/nested WPS/WRF history and postprocessing.

Set `--user` for mounted scratch/store permissions. For Slurm, set
`AV_WRF_MPI_LAUNCHER='srun --mpi=pmix -n'`; the runner appends ranks and executable.
