# `av-wrf`: Cloud-Agnostic Regional WRF Producer Plan

Build a lean, testable regional WRF forecasting producer in `av-wrf` with a pure numerical core and a thin effectful runner inside a pinned Linux OCI container—publishing immutable **Sharded Zarr v3** native model-level forecast stores consumed by `windy-av-layer` (`av-layers`) as `ModelLevelFrame` inputs with zero duplicate soaring science, tiling, or hardcoded regions.

## 1. Scope, Ownership, & Core Guardrails

1. **Strict producer/consumer boundary.** `av-wrf` prepares static geography, initializes and integrates `WRF-ARW`, stages normalized native-model-level Zarr v3 frames, enforces full-horizon and column admission gates, writes immutable `_ready.json` and `_diagnostics.json` cycle receipts under exclusive cycle locks, and triggers `windy-av-layer`. All parcel thermodynamics (`dry-lenschow-1p2-model-levels-v2`), Copernicus GLO-90 subgrid terrain-column refinement, `samples.bin`, `profiles.bin`, and `.pmtiles` stay exclusively in `windy-av-layer`.
2. **Declarative regions in `config/` only (`KISS` / `YAGNI`).**
   - **Initial scope:** One Linux container architecture (`linux/arm64`, pinned by platform OCI digest), declarative `3 km` regional and fast-pilot products under [`config/products/`](../config/README.md) (listed in [`docs/reference.md`](reference.md)), one frozen physics suite, source-owned lateral boundary cadences with verified `forcing_manifest.json`, and explicit model selection in `av-layers`. Phase 4 owns retention, budget alarms, scoped credentials, and rollback.
   - **Deferred until baseline skill and cost are measured:** Additional NH/SH regions (Phase 6), CONUS experiments against operational [NOAA RRFS](https://www.noaa.gov/news-release/noaa-weather-modeling-hits-key-milestone-with-rapid-refresh-forecast-system-debut), `1 km` child nests (`>= 100×100` interior cells), cross-cycle warm land-state splicing, `Auto` model switching, secondary compute backends, and experimental solver engines (`MPAS`, `WRF-JAX`).
3. **Pure numerical core + thin effectful runner.**
   - The pure core handles grid geometry, namelists, vertical checks, thermodynamics, and accumulated fields over each published window.
   - The runner verifies source-specific forcing, runs WPS and WRF, publishes under a filesystem cycle lock, and dispatches webhooks with expiring claims. Shared staging supports replay to multiple destinations.
4. **Provisional -> Consumer-Owned Contract + Independent Oracle.**
   - `vendor/producer-contract/wrf-zarr-v3.schema.json` starts as `"provisional-phase-1"` in `av-wrf` while `windy-av-layer` completes its v5 cutover; Milestone 1 promotes `wrf-zarr-v3.schema.json` + `createZarrV3Provider` into `windy-av-layer/packages/contracts/` (`scripts/sync_contract.py --from-consumer ../windy-av-layer`).
   - Both sides are verified against an independent `PROJ` (`+proj=lcc +R=6370000`) + analytic thermodynamic oracle (`tests/oracle/reference_cases.py`).

## 2. Canonical Output Format

The [producer contract](../vendor/producer-contract/README.md) owns the
Zarr wire schema. The [architecture](architecture.md) describes the staging
and commit path. Publish writes one time step per shard. The inner chunk
index enables tile range reads.

## 3. Frozen Phase 1 Scientific, Vertical, & Schedule Baseline

The [config profiles](../config/README.md) own physics, domain geometry,
forcing cadence, and output timing. The [science notes](science.md) cover
thermodynamic conventions, vertical admission, and diagnostic methods.
Plan checks source arrival and briefing margins for the requested cycle.
The latest pointer also observes cycle age and remaining horizon.

## 4. Execution Sequence & Gates

See the [review evidence](review-plan.md) for validated findings and benchmarks.

| Phase | Scope & Deliverable | Decisive Gate |
| :--- | :--- | :--- |
| **1. Anti-Chaos Harness, Zarr v3 Codec, & Consumer Contract** | Promote `wrf-zarr-v3.schema.json` + `createZarrV3Provider` into `windy-av-layer`; sync via `sync_contract.py --from-consumer` and lock `uv.lock` in `av-wrf`. | `uv run verify` passes from clean checkout; Python `zarr` and the consumer round-trip a fixture; the consumer reads an individual tile by range. |
| **2. Container Solver Build & Cold-Start Slice** | Build the pinned solver image, stage static WPS geography and `forcing_manifest.json`, and run `geog` and `solve`. Qualify private R2 publication with the archived GFS pilot. Replay the paired GFS and ICON-EU pilot. | Real `wrfout` lead and temperature checks, `SolverReceipt` binding, full-horizon `publish`, and staging `av-layers` tiles, `/sample`, and `/profile` pass within budget. |
| **3. Prospective Archiver, Soaring Scorecard, & Latency Baseline** | Run daily prospective archiving of as-issued inputs, `av-layers` baselines (`ICON-EU`, `GFS`), arrival lags, and RAOB/METAR/AWS/IGC observations. | Matched-lead NWP + soaring scorecard ($z_i$, $\Delta \theta_v$, `SHTFL`, PBL shear) runs automatically within regional briefing deadline. |
| **4. Production Hardening, Recovery, & Ops Readiness** | Synchronize `_checkpoint.json` `wrfrst` and `AccumBaseline` restart recovery. Set retention, budget alarms, and scoped credentials. Drill rollback. | Unattended mid-solve restart, `1h->3h` boundary, and post-CAS crash reconciliation drills pass. |
| **5. Controlled Scientific & Product Ablations** | Benchmark `MYNN-EDMF` vs `YSU`, `IFS 0.1°` vs `GFS 0.25°`, [Copernicus DEM `2023_1` `GLO-30`](https://opentopography.org/news/updated-copernicus-30m-DEM-available), `IMS 1 km` snow, [Noah-MP](https://github.com/NCAR/noahmp) land cycling, [OSTIA lake SST](https://help.marine.copernicus.eu/en/articles/4634828-lake-data-available-in-copernicus-marine-products), and `1 km` nests. | Each single-key profile change promotes **only** if paired prospective soaring/NWP scorecard beats the Phase 4 baseline. |
| **6. Multi-Region Onboarding & Secondary Compute** | Onboard additional NH/SH domains via `config/` YAMLs only (zero code edits); qualify secondary runners with independent OCI digests. | Two weeks unattended multi-region SLO compliance. |
