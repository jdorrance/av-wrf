# `av-wrf`: Cloud-Agnostic Regional WRF Producer Plan

Build a lean, testable regional WRF forecasting producer in `av-wrf` with a pure numerical core and a thin effectful runner inside a pinned Linux OCI container—publishing immutable **Sharded Zarr v3** native model-level forecast stores consumed by `windy-av-layer` (`av-layers`) as `ModelLevelFrame` inputs with zero duplicate soaring science, tiling, or hardcoded regions.

## 1. Scope, Ownership, & Core Guardrails

1. **Strict producer/consumer boundary.** `av-wrf` prepares static geography, initializes and integrates `WRF-ARW`, stages normalized native-model-level Zarr v3 frames, enforces full-horizon and column admission gates, writes immutable `_ready.json` and `_diagnostics.json` cycle receipts under exclusive cycle locks, and triggers `windy-av-layer`. All parcel thermodynamics (`dry-lenschow-1p2-model-levels-v2`), Copernicus GLO-90 subgrid terrain-column refinement, `samples.bin`, `profiles.bin`, and `.pmtiles` stay exclusively in `windy-av-layer`.
2. **Declarative regions in `config/` only (`KISS` / `YAGNI`).**
   - **Initial scope:** One Linux container architecture (`linux/arm64`, pinned by platform OCI digest), declarative `3 km` regional and fast-pilot products under [`config/products/`](../config/README.md) (listed in [`docs/reference.md`](reference.md)), one frozen physics suite, source-owned lateral boundary cadences with verified `forcing_manifest.json`, and explicit model selection in `av-layers`. Phase 4 owns retention, budget alarms, scoped credentials, and rollback.
   - **Deferred until baseline skill and cost are measured:** Additional NH/SH regions (Phase 6), CONUS experiments against operational [NOAA RRFS](https://www.noaa.gov/news-release/noaa-weather-modeling-hits-key-milestone-with-rapid-refresh-forecast-system-debut), `1 km` child nests (`>= 100×100` interior cells), cross-cycle warm land-state splicing, `Auto` model switching, secondary compute backends, and experimental solver engines (`MPAS`, `WRF-JAX`).
3. **Pure numerical core + thin effectful runner.**
   - The pure core handles grid geometry, namelists, vertical checks, thermodynamics, and accumulated fields over each published window.
   - The runner verifies GFS inputs, runs WPS and WRF, publishes under a filesystem cycle lock, and dispatches webhooks with expiring claims. Shared staging supports replay to multiple destinations.
4. **Provisional -> Consumer-Owned Contract + Independent Oracle.**
   - `vendor/producer-contract/wrf-zarr-v3.schema.json` starts as `"provisional-phase-1"` in `av-wrf` while `windy-av-layer` completes its v5 cutover; Milestone 1 promotes `wrf-zarr-v3.schema.json` + `createZarrV3Provider` into `windy-av-layer/packages/contracts/` (`scripts/sync_contract.py --from-consumer ../windy-av-layer`).
   - Both sides are verified against an independent `PROJ` (`+proj=lcc +R=6370000`) + analytic thermodynamic oracle (`tests/oracle/reference_cases.py`).

## 2. Canonical Output Format: Sharded Zarr v3 on Native Model Levels (`wrf-zarr-v3`)

`av-wrf` publishes a complete **Zarr v3 (`zarr_format: 3`, [ZEP-0001 / ZEP-0002](https://zarr.dev/zeps/accepted/ZEP0002.html))** store hierarchy directly on WRF's native terrain-following mass levels—mapping 1:1 into `windy-av-layer`'s `ModelLevelFrame`:

- **Store hierarchy per cycle:** Attempt staging under `runs/{product}/{YYYYMMDD}/{HH}/attempts/{id}/staged/` promoted atomically under `cycle_lock` after full-horizon, coordinate, ZEP-0002 payload, and paired pressure-shard verification to `upstream/{model}/{YYYYMMDD}/{HH}/forecast.zarr/` (`zarr.json` root group + 7 coordinate arrays + 22 sharded data arrays = `30` `zarr.json` metadata documents) + `_diagnostics.json` + `_ready.json` (synthetic fixtures stay isolated under `fixtures/synthetic/`).
- **Dimensions & Georeferencing:**
  - `time` (`T` configured lead frames, with `valid_time_unix_s[T]`, `window_start_unix_s[T]`, `window_end_unix_s[T]`), `level` (`K` lowest WRF mass levels), `y` (`nj`), `x` (`ni`).
  - Root `zarr.json` embeds both `crs_wkt` ([GeoZarr](https://github.com/zarr-developers/geozarr-spec)) and the exact `windy-av-layer` `LambertConformalGrid` object.
- **Sharding & Codecs:**
  - `sharding_indexed` stores exact `[64, 64]` inner tiles in NaN-padded outer shards (148×180 → 192×192). Readers clip padding to array shape. The CRC32C index at the end enables range reads of individual zstd tiles.
  - **Precision-bounded IEEE-754 mantissa grooming:** `keepbits = 17` on pressures/heights and `keepbits = 15` on temperatures, humidity (`spfh_kgkg`), winds, and fluxes.

## 3. Frozen Phase 1 Scientific, Vertical, & Schedule Baseline

1. **Physics & Dynamics (`convection-permitting-3km-v1.yaml` + `ForcingProfile`):**
   - `mp_physics = 8` (Thompson), `ra_lw_physics = 4` / `ra_sw_physics = 4` (RRTMG, `radt = 3`, `slope_rad = 1`, `topo_shading = 1`, `shadlen = 25000`), `sf_surface_physics = 4` (`Noah-MP`, `opt_runsrf = 1, opt_runsub = 1`; rejects `5` per [WRF v4.8.0 Known Problems](https://github.com/wrf-model/WRF/wiki/WRF-V4.8.0-Known-Problems)), `bl_pbl_physics = 1` + `sf_sfclay_physics = 1` (`YSU` + Revised MM5, `topo_wind = 1`, `shinhong_scu_mixing = false`, `shinhong_nonlocal_flux = false`), `cu_physics = 0`, `gwd_opt = 0` (`gwd_if_nonhyd = false`), `use_theta_m = 1`, `base_temp = 300.0`, `hybrid_opt = 2` (`etac = 0.2`), `diff_opt = 2`, `km_opt = 4`, `diff_6th_opt = 2` (`0.12`), `epssm = 0.3`, `w_damping = 1`, `damp_opt = 3` (`zdamp = 5000, dampcoef = 0.2`), `time_step = 15`, `spec_bdy_width = 5` (`1 + 4`, `bdy_trim_cells = 8`), `ForcingProfile.interval_seconds` (`3600` GFS, `10800` IFS), `frames_per_outfile = 1`, `history_begin_h = spinup_hours`, `afwa_diag_opt = 1`, `afwa_buoy_opt = 1`, `history_interval = 60`, `bucket_mm = 100.0`, plus `num_metgrid_levels` and `num_metgrid_soil_levels` bound across all staged `met_em` files.
2. **Vertical Grid (`config/profiles/domains/*.yaml`, detailed in [Science and assumptions](science.md)):**
   - [WRF `auto_levels_opt = 2`](https://www2.mmm.ucar.edu/wrf/site/users_guide/real_initialization.html) + `hybrid_opt = 2` (`etac = 0.2`, `e_vert = 55`), `p_top_pa = 5000`, `dzbot_m = 50.0` (keeping `level[0] > 20 m` AGL consumer `minimumLevelAglM` even over `5,137 m` Mount Ararat), `dzstretch_s = 1.11`, `dzstretch_u = 1.06`, `max_dz_m = 850.0` ($\ge 15\%$ thermal-expansion headroom below consumer `maximumProfileGapM = 1000 m`). Verified by `av-wrf check` and `plan`, on `wrfinput_d01` after `real.exe`, and on every `wrfout` frame.
3. **Sensible Air Temperature Semantics (`T` vs `THM`):**
   - Per [WRF Users' Guide Ch. 5](https://www2.mmm.ucar.edu/wrf/users/docs/user_guide_v4/v4.4/users_guide_chap5.html) and [NCAR WRF Forum #5569](https://forum.mmm.ucar.edu/threads/accessing-variables-on-head_grid.5569/), `wrfout` `T` is dry perturbation potential temperature $\theta - 300\text{ K}$, while `THM` is moist perturbation potential temperature $\theta (1 + (R_v/R_d) q_v) - 300\text{ K}$. `codecs/wrf_nc.py` cross-checks `T` and `THM` on every ingested frame.
4. **Deadline, Cron Arrival Buffer, & Live-Pointer Freshness (`core/schedule.py`):**
   - The planner checks the source arrival buffer and the solver margin before the local briefing deadline for the actual cycle date. Live pointer updates also observe that deadline, the cycle age limit, and the remaining horizon.

## 4. Execution Sequence & Gates

See the [adversarial review remediation plan](review-plan.md) for validated findings and benchmarks.

| Phase | Scope & Deliverable | Decisive Gate |
| :--- | :--- | :--- |
| **1. Anti-Chaos Harness, Zarr v3 Codec, & Consumer Contract** | Promote `wrf-zarr-v3.schema.json` + `createZarrV3Provider` into `windy-av-layer`; sync via `sync_contract.py --from-consumer` and lock `uv.lock` in `av-wrf`. | `uv run verify` passes from clean checkout; Python `zarr` and the consumer round-trip a fixture; the consumer reads an individual tile by range. |
| **2. Container Solver Build & Cold-Start Slice** | Build the pinned solver image, stage static WPS geography and `forcing_manifest.json`, and run `geog` and `solve`. Qualify private R2 publication with the archived GFS pilot. Qualify ICON-EU forcing in parallel. | Real `wrfout` lead and temperature checks, `SolverReceipt` binding, full-horizon `publish`, and staging `av-layers` tiles, `/sample`, and `/profile` pass within budget. |
| **3. Prospective Archiver, Soaring Scorecard, & Latency Baseline** | Run daily prospective archiving of as-issued inputs, `av-layers` baselines (`ICON-EU`, `GFS`), arrival lags, and RAOB/METAR/AWS/IGC observations. | Matched-lead NWP + soaring scorecard ($z_i$, $\Delta \theta_v$, `SHTFL`, PBL shear) runs automatically within regional briefing deadline. |
| **4. Production Hardening, Recovery, & Ops Readiness** | Synchronize `_checkpoint.json` `wrfrst` and `AccumBaseline` restart recovery. Set retention, budget alarms, and scoped credentials. Drill rollback. | Unattended mid-solve restart, `1h->3h` boundary, and post-CAS crash reconciliation drills pass. |
| **5. Controlled Scientific & Product Ablations** | Benchmark `MYNN-EDMF` vs `YSU`, `IFS 0.1°` vs `GFS 0.25°`, [Copernicus DEM `2023_1` `GLO-30`](https://opentopography.org/news/updated-copernicus-30m-DEM-available), `IMS 1 km` snow, [Noah-MP](https://github.com/NCAR/noahmp) land cycling, [OSTIA lake SST](https://help.marine.copernicus.eu/en/articles/4634828-lake-data-available-in-copernicus-marine-products), and `1 km` nests. | Each single-key profile change promotes **only** if paired prospective soaring/NWP scorecard beats the Phase 4 baseline. |
| **6. Multi-Region Onboarding & Secondary Compute** | Onboard additional NH/SH domains via `config/` YAMLs only (zero code edits); qualify secondary runners with independent OCI digests. | Two weeks unattended multi-region SLO compliance. |
