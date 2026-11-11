# Architecture

`av-wrf` resolves a declarative regional product into an immutable `RunLock`,
runs `WPS` and `WRF-ARW` inside a pinned Linux container, streams hourly native
model-level shards into a **Zarr v3** store (`forecast.zarr`), writes
`_ready.json` and `_diagnostics.json`, advances `latest.json` via compare-and-swap,
and dispatches a build trigger to `windy-av-layer`.

```mermaid
flowchart LR
  cfg["config/ YAML"] --> plan["plan -> run.lock.json"]
  plan --> geog["geog -> geo_em.d01.nc"]
  geog --> prep["prep -> wrfinput / wrfbdy"]
  prep --> solve["solve -> wrfout + _checkpoint.json"]
  solve --> post["post_frame -> forecast.zarr shards"]
  post --> pub["publish -> _ready.json + latest.json CAS"]
  pub --> av["windy-av-layer ModelLevelFrame"]
```

## Five-layer acyclic package graph

Imports flow strictly top-down (`L4 -> L3 -> L2 -> L1 -> L0`), enforced by
`scripts/check_repo.py`, [`tach.toml`](../tach.toml), and `import-linter`:

```mermaid
flowchart TB
  cli["L4: av_wrf.cli<br/>CLI flags & sole os.environ reader"] --> runner["L3a: av_wrf.runner<br/>stores, mirrors, mpirun, dispatch"]
  cli --> verify["L3b: av_wrf.verify<br/>archiver, obs, scorecards"]
  runner --> codecs["L2: av_wrf.codecs<br/>local NetCDF4 & Zarr v3 shards"]
  verify --> codecs
  codecs --> core["L1: av_wrf.core<br/>grid, schedule, vertical, namelist, forcing, post, metrics"]
  core --> contracts["L0: av_wrf.contracts<br/>frozen schemas, RunLock, NewTypes"]
```

## Codemap

- [`src/av_wrf/contracts/`](../src/av_wrf/contracts/README.md): `ProductSpec`,
  `DomainProfile`, `ScienceProfile`, `ForcingProfile`, `OutputProfile`,
  `CycleWindow`, `RunLock`, `ReadyReceipt`, `DiagnosticsReceipt`,
  `WRF_ZARR_V3_FIELDS`, and nominal unit/stagger types (`TempK`, `EarthWindMps`,
  `OmegaPaS`, `CompMass3D`, `PubMass3D`).
- [`src/av_wrf/core/`](../src/av_wrf/core/README.md): seven peer-isolated modules
  (`grid.py`, `schedule.py`, `vertical.py`, `namelist.py`, `forcing.py`,
  `post.py`, `metrics.py`). Peers may depend on `contracts` and `core.grid` only.
- [`src/av_wrf/codecs/`](../src/av_wrf/codecs/README.md): `wrf_nc.py` reads typed
  `RawWrfFrame` buffers from `wrfout`; `zarr_v3.py` emits root `zarr.json`
  (`LambertConformalGrid` `x0M`/`y0M`) and `sharding_indexed` shard paths.
- [`src/av_wrf/runner/`](../src/av_wrf/runner/README.md): `store.py`
  (`ObjectStore`, `LocalFileStore`, `open_store`), `mirrors.py` (fail-closed
  forcing bundles), `solver.py` (container digest & `_checkpoint.json`),
  `stages.py` (`check_product_config`, `plan_product_cycle`), and `dispatch.py`.
- [`src/av_wrf/verify/`](../src/av_wrf/verify/README.md): `archiver.py`,
  `observations.py`, and `scorecard.py` for mature-lead evaluation.
- [`src/av_wrf/cli/`](../src/av_wrf/cli/README.md): `env.py` (`RuntimeEnv`) and
  `main.py` (`av-wrf check`, `av-wrf plan`, and Phase 2 solver subcommands).

## Boundary invariants and deliberate absences

- **Zero consumer science in `av-wrf`.** Parcel thermodynamics, subgrid DEM
  refinement, `samples.bin`, `profiles.bin`, and `.pmtiles` never appear in
  `src/av_wrf/`.
- **Hermetic `contracts` and `core`.** Both layers forbid file I/O, network
  clients, subprocesses, `netCDF4`, `zarr`, `os`, `sys`, `importlib`, `random`,
  and wall-clock calls (`datetime.now`, `time.time`).
- **Single environment reader.** Only `src/av_wrf/cli/env.py` reads `os.environ`.
- **Mutual isolation at `L3`.** `runner` never imports `verify`; `verify` never
  executes WRF, writes `latest.json`, or dispatches webhooks.
- **Zero type-ignore or mutable records.** Every `@dataclass` in `src/` is
  `frozen=True`; `# type: ignore`, `typing.Any`, `abc.ABC`, and relative imports
  are rejected mechanically.
- **Immutable external truth.** `vendor/producer-contract/`, `docker/patches/`,
  and `tests/oracle/` are locked by SHA-256 in [`manifest.lock`](../manifest.lock).

## External weather & solver specifications

- [WRF v4.8.0 Releases](https://github.com/wrf-model/WRF/releases) &
  [WRF v4.8.0 Known Problems](https://github.com/wrf-model/WRF/wiki/WRF-V4.8.0-Known-Problems)
- [WPS v4.7.0 Releases](https://github.com/wrf-model/WPS/releases) &
  [WPS Best Practices](https://www2.mmm.ucar.edu/wrf/site/documentation/namelist.wps_best_practices.html)
- [WRF Namelist Variables (`auto_levels_opt = 2`)](https://www2.mmm.ucar.edu/wrf/users/wrf_users_guide/build/html/namelist_variables.html) &
  [WRF Users' Guide Ch. 5](https://www2.mmm.ucar.edu/wrf/users/docs/user_guide_v4/v4.4/users_guide_chap5.html)
- [WRF `SINALPHA`/`COSALPHA` Earth Wind Rotation](https://github.com/NCAR/wrf-python/issues/142)
- [ECMWF IFS Open Data](https://www.ecmwf.int/en/forecasts/datasets/open-data),
  [NOAA GFS Open Data](https://registry.opendata.aws/noaa-gfs-bdp-pds/), &
  [Copernicus DEM `DGED 2023_1`](https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM)
- [Zarr v3 Sharding & Codecs](https://zarr.readthedocs.io/en/stable/api/zarr/codecs/) &
  [GeoZarr](https://github.com/zarr-developers/geozarr-spec)
