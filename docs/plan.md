# `av-wrf`: Cloud-Agnostic Regional WRF Producer Plan

Build a lean, testable regional WRF forecasting producer in `av-wrf` with a pure numerical core and a thin effectful runner inside a pinned Linux OCI container—publishing immutable **Sharded Zarr v3** native model-level forecast stores consumed by `windy-av-layer` (`av-layers`) as `ModelLevelFrame` inputs with zero duplicate soaring science or tiling.

## 1. Scope, Ownership, & Core Guardrails

1. **Strict producer/consumer boundary.** `av-wrf` prepares static geography, initializes and integrates `WRF-ARW`, exports normalized native-model-level Zarr v3 frames, passes pre-promotion sanity checks, writes immutable `_ready.json` and `_diagnostics.json` cycle receipts, and triggers `windy-av-layer`. All parcel thermodynamics (`dry-lenschow-1p2-model-levels-v2`), Copernicus GLO-90 subgrid terrain-column refinement, `samples.bin`, `profiles.bin`, and `.pmtiles` stay exclusively in `windy-av-layer` (`docs/wrf-prep-plan.md`, prerequisite: v5 cutover complete on `main`).
2. **Start narrow; earn every expansion with measurements (`KISS` / `YAGNI`).**
   - **Phase 1 production scope:** One Linux container architecture (`linux/arm64`, pinned by platform OCI digest), one cold-start `3 km` product (**`wrf-armenia-3km`**), one frozen physics/vertical-grid suite, one primary forcing recipe with fail-closed validation, explicit model selection in `av-layers`, and Day-1 retention, budget alarms, scoped credentials, and rollback.
   - **Deferred until baseline skill and cost are measured:** Second region (`wrf-nz-3km` in Phase 6), CONUS experiments against operational [NOAA RRFS](https://www.noaa.gov/news-release/noaa-weather-modeling-hits-key-milestone-with-rapid-refresh-forecast-system-debut), `1 km` child nests (`>= 100×100` interior cells), cross-cycle warm land-state splicing, `Auto` model switching, secondary compute backends, and experimental solver engines (`MPAS`, `WRF-JAX`).
3. **Pure numerical core + thin effectful runner (no Nextflow, no over-abstracted framework).**
   - Pure Python functions over contiguous `float32` / NumPy buffers own domain math, `auto_levels_opt = 2` vertical checks, `RunLock` resolution, exhaustive namelist compilation, stateful `post_frame` + `AccumBaseline` differencing, Arakawa-C destaggering, thermodynamic/wind transforms, and verification math.
   - A small effectful runner handles object storage (`file://` and S3/R2), HTTP mirror downloads, `mpirun` execution, and idempotent webhook dispatch (`_dispatched.json`).
4. **Provisional -> Consumer-Owned Contract + Independent Oracle.**
   - `vendor/producer-contract/wrf-zarr-v3.schema.json` starts as `"provisional-phase-1"` in `av-wrf` while `windy-av-layer` completes its v5 cutover; Milestone 1 promotes `wrf-zarr-v3.schema.json` + `createZarrV3Provider` into `windy-av-layer/packages/contracts/` as the canonical owner (`scripts/sync_contract.py --from-consumer ../windy-av-layer`).
   - Both sides are verified against an independent `PROJ` (`+proj=lcc +R=6370000`) + analytic thermodynamic oracle (`tests/oracle/reference_cases.py`).

## 2. Canonical Output Format: Sharded Zarr v3 on Native Model Levels (`wrf-zarr-v3`)

`av-wrf` publishes a complete **Zarr v3 (`zarr_format: 3`, [ZEP-0001 / ZEP-0002](https://zarr.dev/zeps/accepted/ZEP0002.html))** store hierarchy directly on WRF's native terrain-following mass levels—mapping 1:1 into `windy-av-layer`'s `ModelLevelFrame` (`packages/forecast/src/inputs.ts:52`):

- **Store hierarchy per cycle:** `upstream/{model}/{YYYYMMDD}/{HH}/forecast.zarr/` (`zarr.json` root group + 7 coordinate arrays + 22 sharded data arrays = `30` `zarr.json` metadata documents) + `_ready.json` + `_diagnostics.json`.
- **Dimensions & Georeferencing:**
  - `time` (`T = 40` lead frames for 48h `1..36, 39..48`, with `valid_time_unix_s[T]`, `window_start_unix_s[T]`, `window_end_unix_s[T]`), `level` (`K = 40` lowest WRF mass levels), `y` (`nj = 148`), `x` (`ni = 180`).
  - Root `zarr.json` embeds both `crs_wkt` ([GeoZarr](https://github.com/zarr-developers/geozarr-spec)) and the exact `windy-av-layer` `LambertConformalGrid` object (`kind: "lambert-conformal"`, `width`, `height`, `earthRadiusM`, `centralMeridianDeg`, `latitudeOfOriginDeg`, `standardParallel1Deg`, `standardParallel2Deg`, `x0M`, `y0M`, `dxM`, `dyM`, `wrapLongitude: false`).
- **Sharding, Codecs, & Measured Storage Footprint:**
  - **Outer shard shape:** `(shard_time_steps=1, K, nj, ni)` for 3D and `(1, nj, ni)` for 2D—`40 × 21 + 1 = 841` immutable shard objects per cycle (`~1.25 GB` raw `float32`; **`~360–500 MB` compressed** at `2.5×–3.5×` `zstd(level=3)`).
  - **Spec-compliant Zarr v3 codec chain:** `sharding_indexed` (`index_location: "end"`) with inner data codecs `[{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "zstd", "configuration": {"level": 3}}]` (decoded natively via Node 22 `node:zlib.zstdDecompress` and `zarrita.js`—spike-verified in Phase 1) and index codecs `[{"name": "bytes", "configuration": {"endian": "little"}}, {"name": "crc32c"}]`.
  - **Precision-bounded IEEE-754 mantissa grooming:** `keepbits = 17` on pressures/heights (`<= 0.5 Pa` at `100,000 Pa`, `<= 0.03 m` at `4,000 m`) and `keepbits = 15` on temperatures (`<= 0.004 K` at `288 K`), humidity (`spfh_kgkg` as moisture source of truth matching `ICON-EU`/`RRFS`), winds, and fluxes.

### Frozen 22-Array `wrf-zarr-v3` Field Table (1:1 with `ModelLevelFrame`)

| Zarr Array Name | Shape | Units | `ModelLevelFrame` Target | WRF Source & Physical Semantics (`core/post.py`) |
| :--- | :--- | :--- | :--- | :--- |
| `terrain_m` | `(nj, ni)` | `m` | `terrainM` | Static `HGT` (`keepbits=17`) |
| `surface_pressure_pa` | `(T, nj, ni)` | `Pa` | `surfacePressurePa` | Instant `PSFC` (`keepbits=17`) |
| `temp_2m_k` | `(T, nj, ni)` | `K` | `temp2mK` | Instant `T2` sensible air temperature (`keepbits=15`) |
| `spfh_2m_kgkg` | `(T, nj, ni)` | `kg/kg` | `specificHumidity2mKgKg` | Instant `Q2 / (1 + Q2)` Bolton-clamped (`keepbits=15`) |
| `sensible_heat_flux_wm2` | `(T, nj, ni)` | `W/m2` | `sensibleHeatFluxWm2` | Trailing window `(ΔACHFX + ΔI_ACHFX*bucket_J) / Δt` |
| `latent_heat_flux_wm2` | `(T, nj, ni)` | `W/m2` | `latentHeatFluxWm2` | Trailing window `(ΔACLHF + ΔI_ACLHF*bucket_J) / Δt` |
| `precip_mm_hr` | `(T, nj, ni)` | `mm/h` | `precipitationMmPerHour` | Trailing window `3600 * Δ(RAINC + RAINNC + buckets) / Δt` |
| `mslp_pa` | `(T, nj, ni)` | `Pa` | `auxSurface.mslpPa` | WMO / WRF Shuell plateau-clamped MSLP (`keepbits=17`) |
| `u_10m_earth_mps` | `(T, nj, ni)` | `m/s` | `auxSurface.u10mMps` | `U10`/`V10` rotated via `COSALPHA`/`SINALPHA` |
| `v_10m_earth_mps` | `(T, nj, ni)` | `m/s` | `auxSurface.v10mMps` | `U10`/`V10` rotated via `COSALPHA`/`SINALPHA` |
| `gust_10m_mps` | `(T, nj, ni)` | `m/s` | `auxSurface.gust10mMps` | `max(WSPD10MAX, V10 + 7.71*(u*^3 + 0.2/12*w*^3)^(1/3))` |
| `pbl_height_m` | `(T, nj, ni)` | `m` | `auxSurface.pblHeightM` | Instant `PBLH` AGL |
| `cape_jkg` | `(T, nj, ni)` | `J/kg` | `auxSurface.capeJkg` | Instant `AFWA_CAPE` (`afwa_diag_opt=1, afwa_severe_opt=1`) |
| `cloud_total_pct` | `(T, nj, ni)` | `%` | `auxSurface.cloudTotalPct` | Max-random overlap across full `CLDFRA` column (`0..100`) |
| `cloud_low_pct` | `(T, nj, ni)` | `%` | `auxSurface.cloudLowPct` | Max-random overlap on `CLDFRA` where `p >= 0.8 * PSFC` |
| `pressure_pa` | `(T, K, nj, ni)` | `Pa` | `pressurePa[k]` | Instant full pressure `P + PB` (`keepbits=17`) |
| `height_msl_m` | `(T, K, nj, ni)` | `m` | `hgtM[k]` | Destaggered `(PH + PHB) / 9.81` (`keepbits=17`) |
| `temp_k` | `(T, K, nj, ni)` | `K` | `tempK[k]` | Sensible air temperature `(T + base_temp) * (p/p0)^(Rd/cp)` |
| `spfh_kgkg` | `(T, K, nj, ni)` | `kg/kg` | `auxLevels.spfhKgKg[k]` | Instant `QVAPOR / (1 + QVAPOR)` (`rhPercent` omitted per `ICON-EU`/`RRFS`) |
| `u_earth_mps` | `(T, K, nj, ni)` | `m/s` | `auxLevels.uWindMps[k]` | Arakawa-C `U`/`V` destaggered + Earth-rotated |
| `v_earth_mps` | `(T, K, nj, ni)` | `m/s` | `auxLevels.vWindMps[k]` | Arakawa-C `U`/`V` destaggered + Earth-rotated |
| `omega_pas` | `(T, K, nj, ni)` | `Pa/s` | `auxLevels.omegaPaS[k]` | Destaggered `W` via moist density `-ρ_m * g0 * w` |

## 3. Frozen Phase 1 Scientific, Vertical, & Schedule Baseline (`wrf-armenia-3km`)

1. **Physics & Dynamics (`convection-permitting-3km-v1.yaml`):**
   - `mp_physics = 8` (Thompson), `ra_lw_physics = 4` / `ra_sw_physics = 4` (RRTMG, `radt = 3`, `slope_rad = 1`, `topo_shading = 1`, `shadlen = 25000`), `sf_surface_physics = 4` (`Noah-MP`, `opt_runsrf = 1, opt_runsub = 1`; rejects `5` per [WRF v4.8.0 Known Problems](https://github.com/wrf-model/WRF/wiki/WRF-V4.8.0-Known-Problems)), `bl_pbl_physics = 1` + `sf_sfclay_physics = 1` (`YSU` + Revised MM5, `topo_wind = 1`, `shinhong_scu_mixing = 0`, `shinhong_nonlocal_flux = false`), `cu_physics = 0`, `gwd_opt = 0` (`gwd_if_nonhyd = false`), `use_theta_m = 1`, `base_temp = 300.0`, `hybrid_opt = 2` (`etac = 0.2`), `diff_opt = 2`, `km_opt = 4`, `diff_6th_opt = 2` (`0.12`), `epssm = 0.3`, `w_damping = 1`, `damp_opt = 3` (`zdamp = 5000, dampcoef = 0.2`), `time_step = 15`, `spec_bdy_width = 5` (`1 + 4`, `bdy_trim_cells = 8`), `interval_seconds = 3600` (`&time_control`), `nwp_diagnostics = 1`, `afwa_diag_opt = 1`, `afwa_severe_opt = 1`, `history_interval = 60`, `prec_acc_dt = 60`, `bucket_mm = 100.0`, `bucket_J = 1.0e9`.
2. **Vertical Grid (`armenia-3km-v1.yaml`, detailed in [Science and assumptions](science.md)):**
   - [WRF `auto_levels_opt = 2`](https://www2.mmm.ucar.edu/wrf/site/users_guide/real_initialization.html) + `hybrid_opt = 2` (`etac = 0.2`) with `e_vert = 55`, `p_top_pa = 5000`, `dzbot_m = 34.0` ([WRF Forum #8325](https://forum.mmm.ucar.edu/threads/increase-eta-level-and-decrease-dzbot.8325/)), `dzstretch_s = 1.11`, `dzstretch_u = 1.06`, `max_dz_m = 850.0` ($\ge 15\%$ summer thermal-expansion headroom below `windy-av-layer` `maximumProfileGapM = 1000 m`), `exported_levels_k = 40`. Verified by `av-wrf check` to remain fold-free and place $\ge 18$ mass midpoints in `0–2,000 m` AGL across `800 m` Ararat valley, `4,090 m` Mount Aragats, and `5,137 m` Mount Ararat.
3. **Sensible Air Temperature Semantics (`T` vs `THM`):**
   - Per [WRF Users' Guide Ch. 5](https://www2.mmm.ucar.edu/wrf/users/docs/user_guide_v4/v4.4/users_guide_chap5.html) and [NCAR WRF Forum #5569](https://forum.mmm.ucar.edu/threads/accessing-variables-on-head_grid.5569/), `wrfout` `T` is perturbation potential temperature $\theta - T_0$, while `THM` is perturbation moist potential temperature $\theta (1 + (R_v/R_d) q_v) - T_0$. Both yield identical sensible air temperature $T_{\text{air}} = \theta (p / p_0)^{R_d / c_p}$ (`core/post.py` cross-checks `potential_to_temp_k` against `moist_thm_to_temp_k`).
4. **Deadline, Cron Arrival Buffer, & Remaining-Horizon Invariants (`core/schedule.py`):**
   - `18Z GFS` p95 arrival is `4.8 h` (`22:48 UTC`). `schedule_cron_utc = "15 23 * * *"` (`23:15 UTC`) enforces `>= 15 min` arrival buffer (`27 min` margin), runs `6 h` spin-up (`18Z..00Z`) from `f000` `AccumBaseline`, publishes `00Z` (`f001..f048`), caps `max_cycle_age_hours = 6` (`06:00 UTC`), and guarantees `48 - 6 = 42 h >= min_horizon_hours = 36` well ahead of `08:30 Asia/Yerevan` (`04:30 UTC`).

## 4. Operational & Verification Safeguards

- **Fail-closed incomplete-forcing policy & `IFS 0.1°` 14-level gate:** Reject any incomplete or non-finite forcing bundle after mirror retries ([ECMWF Open Data](https://www.ecmwf.int/en/forecasts/datasets/open-data), [ECMWF 0.1° schedule](https://forum.ecmwf.int/t/ecmwf-free-and-open-data-at-9-km-soon-to-come/15366), [NOAA GFS](https://registry.opendata.aws/noaa-gfs-bdp-pds/)); gate `IFS 0.1°` in Phase 2 on `real` `850–700 hPa` plateau interpolation accuracy vs `UGEE` RAOBs.
- **Armenia `GLO-30` (`DGED 2023_1+`) immutable cache:** Mirror [Copernicus DEM `2023_1`](https://dataspace.copernicus.eu/explore-data/data-collections/copernicus-contributing-missions/collections-description/COP-DEM) / [OpenTopography](https://opentopography.org/news/updated-copernicus-30m-DEM-available) tiles into R2 `geog-sources/` pinned by SHA-256 (`N40 E044` is absent on older [AWS S3 mirrors](https://copernicus-dem-90m.s3.amazonaws.com/readme.html)); harvest turnpoint probes from [RASPWeather/rasp-models](https://github.com/RASPWeather/rasp-models), [canadarasp](https://github.com/ajberkley/canadarasp), and [RASP NZ](https://raspnz.com/).
- **Complete `_ready.json`, `_diagnostics.json`, `flock`/CAS fencing, & idempotent dispatch:** `_ready.json` carries `grid`, `lead_hours`, `valid_times_utc`, `window_starts_utc`/`window_ends_utc`, `field_names`, `shard_sha256s`, `config_sha256`, and `oci_digest`; `reconcile_dispatch` short-circuits cleanly if `_dispatched.json` exists.
- **Prospective archiving & soaring proxies (`verify`):** Archive as-issued inputs and baselines; score mature leads on surface/upper-air RMSE plus $z_i$, capping $\Delta \theta_v$, sensible heat flux, and `0–2 km` vector wind shear.

## 5. Authoritative 5-Layer Architecture & Mechanical Anti-Chaos Harness

Grounded in [`dynamical-org/reformatters`](https://github.com/dynamical-org/reformatters), [ECMWF `earthkit`](https://www.ecmwf.int/en/newsletter/179/computing/introducing-earthkit), [matklad's `ARCHITECTURE.md`](https://matklad.github.io/2021/02/06/ARCHITECTURE.md.html), and [OpenAI's Harness Engineering](https://openai.com/ca-ES/index/harness-engineering/):

- **5-Layer DAG (`tach.toml`, `import-linter`, `scripts/check_repo.py`):**
  `L4 av_wrf.cli` (`env.py`, `main.py`) -> `{L3a av_wrf.runner | L3b av_wrf.verify}` -> `L2 av_wrf.codecs` (`wrf_nc.py`, `zarr_v3.py`) -> `L1 av_wrf.core` (`grid`, `schedule`, `vertical`, `namelist`, `forcing`, `post`, `metrics`) -> `L0 av_wrf.contracts` (frozen `@dataclass(frozen=True)` records, `Slab2DF32`/`Slab3DF32` float32 buffers, nominal `NewType`s).
- **Fail-Closed Verification (`uv run verify`):** Executes `check_repo.py` (`<= 350` LOC/file, `<= 60` LOC/func, `<= 100` cols `E501`, zero `Any`, zero `# type: ignore`, zero bare `zip()` `B905`, zero `abc.ABC`, zero relative/`importlib` imports, `manifest.lock` SHA-256 check) -> `generate_reference.py --check` -> `tach check` -> `lint-imports` -> `ruff check` & `format --check` -> `mypy --strict` -> `pytest`. Fails closed if any tool is missing unless `--stdlib-only` is explicitly passed.

## 6. Execution Sequence & Gates

| Phase | Scope & Deliverable | Decisive Gate |
| :--- | :--- | :--- |
| **1. Anti-Chaos Harness, Zarr v3 Codec Spike, & Consumer Contract** | Promote `wrf-zarr-v3.schema.json` + `createZarrV3Provider` (`zarrita.js` / `node:zlib.zstdDecompress`) into `windy-av-layer` (post-v5 cutover); sync via `sync_contract.py --from-consumer` and lock `uv.lock` in `av-wrf`. | `uv run verify` passes from clean checkout; Node 22 `zarrita` + Python `zarr`/`xarray.open_zarr` round-trip a real sharded `forecast.zarr` + `_ready.json` fixture (`minHorizonHours: 36`). |
| **2. Container Solver Build & Cold-Start `wrf-armenia-3km` Slice** | Compile pinned [WRF v4.8.0](https://github.com/wrf-model/WRF/releases) + [WPS v4.7.0](https://github.com/wrf-model/WPS/releases) (`linux/arm64` + SHA-256 patches + `build-manifest.json`); mirror `DGED 2023_1` `GLO-30`; test `IFS 0.1°` vs `GFS 0.25°` `real` soundings; wire `geog`, `prep`, `solve`, NetCDF->Zarr `post`, and `publish`. | Real `wrfout` `T`/`THM` fixture check, `f000` `AccumBaseline` -> `f001..f048` (`1h->3h` tail), vertical BL gate (`>= 18` levels `< 2 km` AGL over Aragats), and live staging `av-layers` tiles/`/sample`/`/profile` pass within budget. |
| **3. Prospective Archiver, Soaring Scorecard, & Latency Baseline** | Run daily prospective archiving of as-issued inputs, `av-layers` baselines (`ICON-EU`, `GFS`), arrival lags, and `UGEE` RAOB/METAR/AWS/IGC data; measure compressed shard bytes and p95 runtime. | Matched-lead NWP + soaring scorecard ($z_i$, $\Delta \theta_v$, `SHTFL`, PBL shear) runs automatically; p95 runtime + arrival lag fits `08:30 Asia/Yerevan` deadline. |
| **4. Production Hardening, Recovery, & Ops Readiness** | S3/R2 `obstore` adapter (`If-None-Match` + `If-Match` CAS), synchronized `_checkpoint.json` `wrfrst` + `AccumBaseline` restart recovery, retention cleanup, scoped secrets, budget alarms, and rollback drill. | Live mid-solve kill/restart, `1h->3h` restart boundary, partial upload, post-CAS crash reconciliation, incomplete-forcing fail-closed drill, and rollback drills pass unattended. |
| **5. Controlled Scientific & Product Ablations (One at a Time)** | Benchmark `MYNN-EDMF` vs `YSU`, `IFS 0.1°` vs `GFS 0.25°`, `WorldCover`/`SoilGrids` vs stock MODIS, `IMS 1 km` snow, full [Noah-MP](https://github.com/NCAR/noahmp) `wrfinput` land cycling, [OSTIA/Lake Sevan SST](https://help.marine.copernicus.eu/en/articles/4634828-lake-data-available-in-copernicus-marine-products), `1 km` nest, and `Auto` rules. | Each single-key profile change promotes **only** if paired prospective soaring/NWP scorecard + `_diagnostics.json` water/energy checks beat the Phase 4 baseline. |
| **6. Second Region (`wrf-nz-3km`) & Secondary Compute/R&D** | Onboard `wrf-nz-3km` (SH Lambert west of `180°`, DST-aware `Pacific/Auckland`); qualify `linux/amd64` / Spot runner with own OCI digest; keep `MPAS` / `WRF-JAX` in separate R&D track. | Two weeks unattended multi-region SLO compliance; CONUS entered only if archived `RRFS` baseline gap justifies compute. |
