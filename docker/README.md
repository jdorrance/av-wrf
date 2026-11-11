# Container Build (`docker/`)

Builds the runnable `av-wrf` Python producer container (`av-wrf check`, `av-wrf plan`).
Phase 2 adds the upstream `WRF v4.8.0` + `WPS v4.7.0` CMake solver stage and
`/opt/av-wrf/build-manifest.json`.

```sh
docker buildx build --platform linux/arm64 -f docker/Dockerfile -t av-wrf:local .
docker run --rm av-wrf:local check --product wrf-armenia-3km
```

Region configs and credentials stay mounted or declarative; solver patches in
[`patches/`](patches/README.md) are hashed in [`manifest.lock`](../manifest.lock).
