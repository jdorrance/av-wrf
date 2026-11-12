# Architecture

`av-wrf` resolves a declarative product in `config/` into an immutable
`RunLock`, writes Fortran namelists plus a source-cadence forcing cutout recipe
during `prep`, verifies `forcing_manifest.json` and runs `WPS` + `WRF-ARW` inside
a pinned Linux container, stages `Times`-indexed native model-level shards with
`[64, 64]` inner chunks under an attempt prefix, verifies the complete `RunLock`
horizon under an exclusive per-cycle lock before promoting `forecast.zarr`,
`_diagnostics.json`, and `_ready.json`, advances `latest.json` via opaque-token
compare-and-swap, and dispatches a consumer webhook with expiring leases.

```mermaid
flowchart LR
  cfg["config/ YAML"] --> geog["geog -> geo_em.d01.nc"]
  geog --> plan["plan -> run.lock.json"]
  plan --> prep["prep -> namelists + forcing_subset.json"]
  prep --> forcing["forcing-manifest -> verified local GRIB2 inventory"]
  forcing --> solve["solve -> met_em + validated wrfinput + wrfout"]
  solve --> post["post -> attempt staged/forecast.zarr"]
  post --> pub["publish -> cycle_lock + verify horizon + _ready.json + CAS"]
  pub --> av["windy-av-layer ModelLevelFrame"]
```

## Five-layer acyclic package graph

Imports flow strictly top-down (`L4 -> L3 -> L2 -> L1 -> L0`), enforced by
`scripts/check_repo.py`, [`tach.toml`](../tach.toml), and `import-linter`:

```mermaid
flowchart TB
  cli["L4: av_wrf.cli<br/>CLI flags & sole os.environ reader"] --> runner["L3a: av_wrf.runner<br/>stores, stages, post, publish, solver, dispatch"]
  cli --> verify["L3b: av_wrf.verify<br/>sounding scorecards"]
  runner --> codecs["L2: av_wrf.codecs<br/>wrf_nc, zarr_shard, zarr_v3"]
  verify --> codecs
  codecs --> core["L1: av_wrf.core<br/>grid, thermo, schedule, vertical, namelist, post, metrics"]
  core --> contracts["L0: av_wrf.contracts<br/>frozen schemas, RunLock, slabs, NewTypes"]
```

## Codemap

- [`src/av_wrf/contracts/`](../src/av_wrf/contracts/README.md): `ProductSpec`,
  `DomainProfile`, `ScienceProfile`, `ForcingProfile` (`vtable_name`,
  `interval_seconds`), `OutputProfile`, `CycleWindow`, `RunLock`,
  `ForcingManifest`, `SolverReceipt`, `ReadyReceipt`, `DiagnosticsReceipt`,
  `WRF_ZARR_V3_FIELDS`, `Slab2DF32`/`Slab2DI32`/`Slab3DF32`, and nominal types.
- [`src/av_wrf/core/`](../src/av_wrf/core/README.md): pure deterministic modules
  (`grid.py`, `thermo.py`, `schedule.py`, `vertical.py`, `namelist.py`,
  `post.py`, `metrics.py`). These modules perform pure array and scalar math.
- [`src/av_wrf/codecs/`](../src/av_wrf/codecs/README.md): `wrf_nc.py` and
  `wrf_nc_grid.py` reject NetCDF masks/fills, slice `0:k_exp` hyperslabs, verify
  `ZNW` and `XLAT`/`XLONG`, and require `AFWA_CAPE_MU`.
  `zarr_shard.py` and `zarr_v3.py` encode ZEP-0002
  padded outer shards with exact `[64, 64]` inner spatial chunks.
- [`src/av_wrf/runner/`](../src/av_wrf/runner/README.md): `store.py`
  (`ObjectStore`, `LocalFileStore`, `cycle_lock`, `get_with_token`,
  `sha256_file`), `stages.py` (`check`, `plan`, `prep`), `mirrors.py`
  (forcing recipe registry and manifest verification), `forcing_gfs.py` and
  `forcing_icon.py` (source availability, acquisition and WPS inventory), `post.py`,
  `publish.py` + `publish_verify.py` (cycle-locked promotion, decoded column
  admission, stage retention), `solver.py` + `solver_exec.py`, and
  `dispatch.py` (expiring-lease webhook delivery).
- A storage adapter must provide opaque conditional-write tokens and exclusive
  cycle ownership through the ready commit. The implemented backend uses a
  shared filesystem. A remote adapter needs equivalent fencing or a consumer
  contract for committing an attempt-specific payload.
- Local writes sync file contents and directory entries. Prefix deletion syncs
  the surviving parent. Store-private lock files survive prefix replacement.
  Before `_ready.json` commits, the staged manifest digest identifies which
  attempt bytes own a partial promotion.
- Published objects have independent storage from staged objects. Keep staging
  for replay to additional destinations. Pass `publish --prune-stage` on the
  final promotion when no further replay needs the staged payload. Post,
  promotion, and prune share an attempt lock acquired before the destination lock.
- WRF boundary forcing enters through the GRIB recipe. A direct native model
  needs its own reader and grid mapping into published fields before reusing
  the Zarr and promotion path.
- [`src/av_wrf/verify/`](../src/av_wrf/verify/README.md): sounding records and scorecards.
- [`src/av_wrf/cli/`](../src/av_wrf/cli/README.md): `env.py` (`RuntimeEnv`) and
  `main.py` (`av-wrf` subcommands with lazy store handles and `--store-uri` /
  `--prune-stage`).

## Boundary invariants and deliberate absences

- **Region-free code & Dockerfile.** Regions, coordinates, terrain checkpoints,
  and product IDs exist exclusively in `config/`, enforced by `check_repo.py`.
- **Zero consumer science in `av-wrf`.** Parcel thermodynamics, subgrid DEM
  refinement, `samples.bin`, `profiles.bin`, and `.pmtiles` never appear in
  `src/av_wrf/`.
- **Hermetic `contracts` and `core`.** Both layers forbid file I/O, network
  clients, subprocesses, `netCDF4`, `zarr`, `os`, `sys`, `importlib`, `random`,
  and wall-clock calls.
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
- [Zarr v3 Sharding & Codecs](https://zarr.readthedocs.io/en/stable/api/zarr/codecs/) &
  [GeoZarr](https://github.com/zarr-developers/geozarr-spec)
