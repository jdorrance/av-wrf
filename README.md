# `av-wrf`

Regional `WRF-ARW` producer emitting immutable **Sharded Zarr v3** native
model-level forecast stores (`wrf-zarr-v3`), `_ready.json`, and
`_diagnostics.json` for the `windy-av-layer` `ModelLevelFrame` pipeline.

Configured products live under [config/](config/README.md) (`config/products/*.yaml`,
listed in [Generated reference](docs/reference.md)) and bind reusable domain,
science, source, and output profiles into an immutable `run.lock.json`. All
parcel thermodynamics, subgrid DEM columns, sample packs, and PMTiles remain in
`windy-av-layer`.

## Quick start

Use `uv` with Python 3.12 (`.python-version`). Run from the repository root:

```sh
uv sync --locked
uv run av-wrf check
export AV_WRF_STORE_URI=file:///tmp/av-wrf-store
uv run av-wrf plan --product <product-id> --cycle 2026-11-12T00:00:00Z --attempt att-01 --allow-unpinned-dev
uv run verify
```

Run the structural checks alone with system Python:

```sh
python3 scripts/check_repo.py
```

## Project layout

- [src/av_wrf/contracts](src/av_wrf/contracts/README.md): frozen schemas,
  `RunLock`, `AccumBaseline`, `ReadyReceipt`, 22-field Zarr v3 template, and
  contiguous `float32`/`int32` slab types.
- [src/av_wrf/core](src/av_wrf/core/README.md): pure deterministic spherical
  Lambert math, schedule/horizon math, `auto_levels_opt=2` column checks,
  WRF v4.8 namelist compilation, `post_frame`, and metrics.
- [src/av_wrf/codecs](src/av_wrf/codecs/README.md): `Times`-indexed `netCDF4`
  `wrfout` reader, deterministic ZEP-0002 `zstd`+`crc32c` shard codec, and
  `wrf-zarr-v3` metadata.
- [src/av_wrf/runner](src/av_wrf/runner/README.md): `flock`-serialized local storage,
  GFS and ICON-EU forcing recipes, attempt-staged `post`, full-horizon `publish`, and
  idempotent webhook dispatch.
- [src/av_wrf/verify](src/av_wrf/verify/README.md): sounding-based soaring proxies.
- [src/av_wrf/cli](src/av_wrf/cli/README.md): `av-wrf` CLI entrypoint and sole
  `os.environ` reader.
- [config](config/README.md): sole owner of declarative regional product and
  scientific profile YAMLs.
- [vendor/producer-contract](vendor/producer-contract/README.md): provisional
  Phase 1 `wrf-zarr-v3` wire contract to be promoted into `windy-av-layer`.
- [docker](docker/README.md): default Python `runtime` image plus `--target solver`
  compiling pinned `WRF v4.8.0`, `WPS v4.7.0`, and `Jasper 1.900.29`.

## More information

- [AGENTS.md](AGENTS.md): subtractive simplicity rules and hard repository budgets.
- [Architecture](docs/architecture.md): 5-layer DAG, codemap, and boundary invariants.
- [Science and assumptions](docs/science.md): WRF vs. `windy-av-layer` vs. RASP equations,
  constants, and high-terrain approximations.
- [Plan](docs/plan.md): scientific baseline, contracts, and phased execution gates.
- [Generated reference](docs/reference.md): CLI commands, 22-array Zarr table, and
  configured products (`scripts/generate_reference.py`).

License: [MIT](LICENSE). Upstream software and dataset notices are recorded in
[THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).
