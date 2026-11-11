# `av-wrf`

Regional `WRF-ARW` producer emitting immutable **Sharded Zarr v3** native
model-level forecast stores (`wrf-zarr-v3`), `_ready.json`, and
`_diagnostics.json` consumed by `windy-av-layer` (`av-layers`) as
`ModelLevelFrame` inputs.

Configured products live in [config/products](config/products/wrf-armenia-3km.yaml)
and bind reusable [domain, science, source, and output profiles](config/README.md)
into an immutable `run.lock.json`. All parcel thermodynamics, subgrid DEM
columns, sample packs, and PMTiles remain in `windy-av-layer`.

## Quick start

Use `uv` with Python 3.12 (`.python-version`). Run from the repository root:

```sh
uv sync
uv run av-wrf check --product wrf-armenia-3km
uv run av-wrf plan --product wrf-armenia-3km --cycle 2026-11-12T00:00:00Z --attempt att-01
uv run verify
```

`uv run verify` fails closed if `tach`, `import-linter`, `ruff`, `mypy`, or
`pytest` are missing. For bare-OS environments without `uv`, run the stdlib
structural/reference/unittest subset explicitly:

```sh
python3 scripts/verify.py --stdlib-only
```

## Project layout

- [src/av_wrf/contracts](src/av_wrf/contracts/README.md): frozen schemas,
  `RunLock`, `AccumBaseline`, `ReadyReceipt`, 22-field Zarr v3 template, and
  contiguous `float32` slab types.
- [src/av_wrf/core](src/av_wrf/core/README.md): pure deterministic spherical
  Lambert math, schedule/horizon math, `auto_levels_opt=2` column checks,
  WRF v4.8 namelist compilation, soil interpolation, `post_frame`, and metrics.
- [src/av_wrf/codecs](src/av_wrf/codecs/README.md): local `netCDF4` frame I/O
  and complete `wrf-zarr-v3` group/coordinate/array store metadata.
- [src/av_wrf/runner](src/av_wrf/runner/README.md): `flock`-serialized local and
  S3/R2 stores, fail-closed forcing bundles, stage wiring, and idempotent
  webhook dispatch.
- [src/av_wrf/verify](src/av_wrf/verify/README.md): prospective input/baseline
  archiver and mature-lead NWP + soaring-proxy scorecards.
- [src/av_wrf/cli](src/av_wrf/cli/README.md): `av-wrf` CLI entrypoint and sole
  `os.environ` reader.
- [config](config/README.md): declarative product and scientific profile YAMLs.
- [vendor/producer-contract](vendor/producer-contract/README.md): provisional
  Phase 1 `wrf-zarr-v3` wire contract to be promoted into `windy-av-layer`.
- [docker](docker/README.md): runnable `av-wrf` Python container base (Phase 2
  adds the pinned `WRF v4.8.0` + `WPS v4.7.0` CMake solver build).

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
