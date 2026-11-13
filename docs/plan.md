# `av-wrf`: Cloud-Agnostic Regional WRF Producer Plan

Build a lean, testable regional WRF forecasting producer in `av-wrf` with a pure numerical core and a thin effectful runner inside a pinned Linux OCI container—publishing immutable **Sharded Zarr v3** native model-level forecast stores consumed by `windy-av-layer` (`av-layers`) as `ModelLevelFrame` inputs with zero duplicate soaring science, tiling, or hardcoded regions.

## 1. Scope, Ownership, & Core Guardrails

1. **Strict producer/consumer boundary.** `av-wrf` prepares static geography, initializes and integrates `WRF-ARW`, stages normalized native-model-level Zarr v3 frames, enforces full-horizon and column admission gates, writes immutable `_ready.json` and `_diagnostics.json` cycle receipts under exclusive cycle locks, and triggers `windy-av-layer`. All parcel thermodynamics (`dry-lenschow-1p2-model-levels-v2`), Copernicus GLO-90 subgrid terrain-column refinement, `samples.bin`, `profiles.bin`, and `.pmtiles` stay exclusively in `windy-av-layer`.
2. **Declarative regions in `config/` only (`KISS` / `YAGNI`).**
   - **Initial scope:** One Linux container architecture (`linux/arm64`, pinned by platform OCI digest), declarative `3 km` regional and fast-pilot products under [`config/products/`](../config/README.md) (listed in [`docs/reference.md`](reference.md)), one frozen physics suite, source-owned lateral boundary cadences with verified `forcing_manifest.json`, and explicit model selection in `av-layers`. Phase 4 owns retention, budget alarms, scoped credentials, and rollback.
   - **Deferred until baseline skill and cost are measured:** Additional NH/SH regions (Phase 6), CONUS experiments against operational [NOAA RRFS](https://www.noaa.gov/news-release/noaa-weather-modeling-hits-key-milestone-with-rapid-refresh-forecast-system-debut), operational promotion of `1 km` child nests (implementation sequence below), cross-cycle warm land-state splicing, `Auto` model switching, secondary compute backends, and experimental solver engines (`MPAS`, `WRF-JAX`).
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

See the [review evidence](review-plan.md) for validated findings and benchmarks,
and [feedback disposition](review-remediation.md#feedback-disposition-2026-11-13)
for accepted fixes, corrected claims and deferred work.

| Phase | Scope & Deliverable | Decisive Gate |
| :--- | :--- | :--- |
| **1. Anti-Chaos Harness, Zarr v3 Codec, & Consumer Contract** | Promote `wrf-zarr-v3.schema.json` + `createZarrV3Provider` into `windy-av-layer`; sync via `sync_contract.py --from-consumer` and lock `uv.lock` in `av-wrf`. | `uv run verify` passes from clean checkout; Python `zarr` and the consumer round-trip a fixture; the consumer reads an individual tile by range. |
| **2. Container Solver Build & Cold-Start Slice** | Build the pinned solver image, stage static WPS geography and `forcing_manifest.json`, and run `geog` and `solve`. Qualify private R2 publication with the archived GFS pilot. Replay the paired GFS and ICON-EU pilot. | Real `wrfout` lead and temperature checks, `SolverReceipt` binding, full-horizon `publish`, and staging `av-layers` tiles, `/sample`, and `/profile` pass within budget. |
| **3. Prospective Archiver, Soaring Scorecard, & Latency Baseline** | Run daily prospective archiving of as-issued inputs, `av-layers` baselines (`ICON-EU`, `GFS`), arrival lags, and RAOB/METAR/AWS/IGC observations. | Matched-lead NWP + soaring scorecard ($z_i$, $\Delta \theta_v$, `SHTFL`, PBL shear) runs automatically within regional briefing deadline. |
| **4. Production Hardening, Recovery, & Ops Readiness** | Synchronize `_checkpoint.json` `wrfrst` and `AccumBaseline` restart recovery. Set retention, budget alarms, and scoped credentials. Drill rollback. | Unattended mid-solve restart, `1h->3h` boundary, and post-CAS crash reconciliation drills pass. |
| **5. Controlled Scientific & Product Ablations** | Benchmark `MYNN-EDMF` vs `YSU`, `IFS 0.1°` vs `GFS 0.25°`, [Copernicus DEM `2023_1` `GLO-30`](https://opentopography.org/news/updated-copernicus-30m-DEM-available), `IMS 1 km` snow, [Noah-MP](https://github.com/NCAR/noahmp) land cycling, [OSTIA lake SST](https://help.marine.copernicus.eu/en/articles/4634828-lake-data-available-in-copernicus-marine-products), and `1 km` nests. | Each single-key profile change promotes **only** if paired prospective soaring/NWP scorecard beats the Phase 4 baseline. |
| **6. Multi-Region Onboarding & Secondary Compute** | Onboard additional NH/SH domains via `config/` YAMLs only (zero code edits); qualify secondary runners with independent OCI digests. | Two weeks unattended multi-region SLO compliance. |

## 5. Configurable Nesting Implementation Plan

Proposed on 2026-11-13 and revised after [operator and research review](review-plan.md#external-nesting-evidence-researched-2026-11-13).
Implementation and forecast experiments remain pending.
Make fixed WRF nests reusable through configuration, then qualify their weather
skill and cost. Useful targets include groups of flying sites, ridges and valleys,
passes, lake/coastal breezes, snow transitions, and recurring convergence areas.

### Configuration and ownership

- Represent one rooted domain tree with stable names and explicit parent links.
  Support telescoping chains and several non-overlapping children of one parent.
  Start with one-way feedback, 3:1 refinement, one Lambert projection, and common
  vertical coordinates. These are initial project limits; WRF supports more.
- Keep layouts under `config/profiles/domains/`. The root owns projection and
  coarse geometry. Children declare a parent, requested geographic center and
  coverage size, refinement ratio, and boundary buffer. Derive child spacing and
  integer parent offsets once. Geography defaults inherit; explicit dataset
  choices remain available for a fine domain.
- Keep the existing product entry as the execution definition: one source,
  science configuration, cycle, spin-up, horizon, and output schedule. New-format
  products bind an ordered list of publication targets to domain names and unique
  model IDs. Each target owns its crop and exported-level selection. Intermediate
  domains can be published alongside fine domains. Resolve execution as the union
  of targets and their ancestor chains; report excluded branches before locking.
  One-way sibling branches provide no information to an unconnected target.
- Normalize legacy single-output products at the loading boundary. The runtime
  has one domain tuple and one target tuple; retain old raw config and receipt
  bytes for replay with their frozen image and configuration. Version new lock
  documents and freeze the complete resolved layout, target bindings, and input
  hashes. A source change must never silently
  insert or remove a domain from an explicit layout.
- Use one pure resolver in `core.nesting` for WPS, WRF, geography, forcing bounds,
  and publication geometry. Derive WRF domain numbers from the configured order
  after validating that parents precede children. Keep the existing five layers;
  implement the layout with frozen records and plain functions.

### Geometry and scientific admission

- Snap requested coverage outward onto the parent grid. Report requested and
  resolved footprints, grid dimensions, offsets, and publication crop in `plan`.
  Keep the requested area inside the published interior after buffering/cropping.
  Resolve off-center children in the root projection before exporting metadata.
  Check parent-start indexing and staggered half-cell offsets against native
  coordinates; distinguish each child's center from the mother-domain center.
- Reject unknown parents, cycles, duplicate names, overlapping siblings, invalid
  ratios, containment failures, insufficient configured margins, empty crops,
  unsupported build limits, and unsafe MPI tile sizes. Preserve the current
  minimum of 100 computational mass cells per axis. At 1 km this already means
  roughly 100 km across; group nearby sites into a useful meteorological domain.
- Distinguish a child's buffer around its target area, its clearance from the
  parent's boundary, and its publication crop. Check all three. Review boundaries
  against terrain and prevailing flow; geometric admission alone cannot establish
  adequate meteorological coverage. Follow [WPS placement guidance][nest-wps].
- Treat global and per-domain physics entries according to the pinned WRF
  Registry. Initially share the qualified suite; add only typed, tested overrides
  required by a comparison. Review convection on the coarse parent and PBL mixing
  near 1 km. Common land/microphysics choices simplify cross-domain consistency.
- Preserve existing adaptive runs during migration. Qualify new chains and
  sibling layouts first with fixed steps checked on every domain, including map
  factors. Enable adaptive stepping after testing the chosen controlling domain,
  every child's CFL behavior, and exact output times. The last domain number is
  not a general stability criterion. The pinned solver's parent-step adjustment
  from a selected child has a two-domain special case; test larger trees against
  [its implementation][nest-adaptive]. Start larger-tree adaptive trials with
  `adaptation_domain=1` and explicit per-domain limits; time alignment can shorten
  steps below those configured limits. [WRF settings][nest-wrf] own solver semantics.
- Audit selected terrain/land-use sources per domain, including DEM vertical
  datum, interpolation, smoothing, lake/ocean classification, and surface state.
  Verify sea classification on children whose inherited ocean seeds fall outside
  their extent; require suitable in-grid seeds or another verified classification
  strategy and check native `LAKEMASK`. Parent classification alone is insufficient.
  Check initialized snow cover, depth, and water equivalent separately. Finer
  atmospheric grids retain errors in coarse initial snow unless initialization
  improves too. Apply column, radiation, and native-log gates to every domain.
  Trace surface fields and flags from forcing through `met_em`, `wrfinput`, and
  early history; test SWE/depth/density consistency and evolution through spin-up.
  Require finite admitted fields even when native logs report successful completion.

### Execution, publication, and cost

- Acquire forcing for the resolved outer domain once. For the initial physics
  scope, generate all boundary times on the root and initialization inputs on
  children. Explicitly reject SST-update/nudging configurations until their full
  child-time input requirements are implemented. Hash an exact expected geography
  inventory; preserve archive-member
  validation when extending beyond two files. Verify source selection separately
  for every domain so one successful selection cannot conceal another's fallback.
- Execute the tree in one WRF integration. The solver receipt binds histories by
  domain and valid time. Emit required fields on every publication target, retain
  each target's accumulator baseline, and process one target/frame at a time.
  Limit support-domain history to what validation actually consumes.
- Publish each target as its own existing 22-array `forecast.zarr`, ready receipt,
  diagnostics, and latest pointer. Scope staging, recovery, cleanup, and locks by
  target; retain one shared run/attempt solve lock and immutable solver evidence.
  Target cleanup must preserve shared inputs and evidence needed by other targets
  or replay. Keep commits independent and test partial publication recovery.
  Selection/blending between grids belongs to
  `windy-av-layer`; mixed-resolution arrays and new snow fields are separate work.
- A shared WRF failure can affect every target. Keep the established regional
  schedule while experimental layouts are qualified. Successful native runs can
  recover publication per target; a failed solve cannot promise a complete parent
  forecast. Record execution cost once, with references from target diagnostics.
- Extend `plan` with per-domain cell/level counts, timestep ratios, expected
  history/storage volume, and estimated work proportional to
  `sum(nx * ny * nz * duration / timestep)`. At equal area, levels, and duration,
  3 km to 1 km requires roughly 27 times the work for that domain. This is a work
  estimate; calibrated wall-time and memory ranges require native measurements.
- Admit configured domain/cell/storage limits before downloading inputs. Check
  the execution allocation against the requested 12 CPUs and 75 GiB, with memory
  headroom. Keep existing native timeouts and add a total-run deadline. Report
  uncalibrated estimates explicitly; measure peak memory, actual steps, core-hours,
  and deadline margin during pilots. Coarser output cadence saves I/O, while
  smaller area and shorter integration reduce solver work.
  Benchmark the same full tree at 6 and 12 MPI ranks, including I/O; the smallest
  nest can limit parallel efficiency. Repeat the short benchmark before choosing.

For scale, the current 222-by-192 mass-cell regional domain and 134-by-124 parent
give an illustrative work ratio of about 1.65 for one 102-by-102-cell 1 km focus,
or 2.30 for two. This calculation assumes equal vertical levels and duration,
3:1 timesteps, and work proportional to cell-steps. It predicts neither elapsed
time nor memory. Recompute from the resolved layout and calibrate with native
runs. Current measured solver time dominates postprocessing; prioritize that cost.

### Implementation sequence and acceptance

Before new forecast experiments, freeze a case/observation inventory, the current
baseline, and the scoring rules described below. Missing observations leave the
affected phenomenon unqualified while numerical implementation can proceed.

1. **Unify existing execution.** Introduce the resolved domain record in
   `contracts`; use it in `core/nesting.py`, both namelist compilers, and geography
   planning. Replace duplicated one/two-domain decisions. Gate: existing single
   and concentric-pair namelists, published grids, forcing footprints, and archived
   lock/receipt hashes retain their current meaning.
2. **Add explicit layouts and preview.** Extend strict config parsing and
   `runner/bundle.py` / `runner/stages.py`; resolve off-center children, budgets,
   and versioned locks. Gate: a three-level chain and a branched layout produce
   correct parent-aligned geometry; malformed layouts fail before native work.
   Tests cover both hemispheres and compare coordinates to the independent oracle.
3. **Run and verify every domain.** Generalize `core/physics_namelist.py`,
   `runner/geog.py`, `runner/solver_exec.py`, and solver receipts. Gate: pinned
   WPS/real/WRF smoke runs for a three-level chain and two sibling nests verify
   parent IDs, latitude/longitude, rotation, eta levels, surface fields, required
   diagnostics, exact valid times, and clean native CFL logs.
4. **Publish several targets from one solve.** Generalize target selection in
   `runner/post.py` and publication/recovery paths. Gate: an intermediate grid
   and two fine grids publish independently with correct identities, coordinates,
   accumulations, field hashes, and unchanged consumer wire schema. Exercise
   swapped-domain input, missing frames, tampering, partial commits, and retries.
5. **Qualify the first mountain layout.** Compare 3 km and 1 km geography before
   forecasting. Run a short, matched-forcing comparison covering spin-up plus a
   full diurnal cycle. Execute the controlled comparisons below and measure costs
   on the intended allocation before a full-horizon trial.
6. **Prove reuse and promote.** Configure a second region or a lake/coastal focus
   with YAML only. Update generated reference docs and operational instructions.
   Gate operational promotion on predefined skill/non-regression thresholds and
   measured briefing-deadline and resource budgets. Preserve baseline profiles
   for rollback; finer grid spacing alone does not satisfy the skill gate.

Every implementation increment runs `uv run verify` and `git diff --check`.
Native runs add evidence beyond those checks. Keep independent oracle tolerances
fixed and obey file/function budgets by removing obsolete pair-specific branches.

### Evidence-driven experiment sequence

The [external evidence](review-plan.md#external-nesting-evidence-researched-2026-11-13)
changes qualification from a single resolution trial to the following staged
comparisons. Run each on a small common verification area before broadening it.

1. **Terrain audit:** inspect summits, valley cross-sections/volume, passes,
   shorelines, land cover, and exposure at evaluation sites. Record source-DEM,
   model-grid, and observed elevations with their datums. Select coverage around
   the connected valley/ridge flow system and its upwind approaches.
2. **Resolution:** retain the same physics, source stream, static datasets, and
   parent timestep policy. Compare the 3 km parent and 1 km child from a one-way
   run at common sites/times; also aggregate the child onto the parent grid.
   Check the parent against a two-domain control using matching fixed timesteps
   to isolate nesting effects. Separately compare against the existing adaptive
   operational baseline; that comparison also changes timestep policy.
3. **Terrain source:** change the fine-domain DEM separately with parents held
   fixed. If attribution remains unclear, run the fine grid with parent-smoothed
   terrain as a research control. Keep land-cover changes separate as well.
4. **Footprint:** expand or shift the child while keeping a shared interior
   evaluation area. Test weak diurnal flow and strong cross-mountain flow; compare
   pressure gradients, wind vectors, and circulation onset. This tests whether
   the initial domain is too dependent on its boundaries.
5. **Initialization:** compare 6-, 12-, and 24-hour spin-up over the same valid
   times using one forcing cycle with sufficient history. Record initial-state
   and forecast-evolution errors separately. Then test the selected duration
   against normal operational source availability and age; a controlled spin-up
   result alone cannot establish the best live-cycle policy.
6. **Physics/source sensitivity:** after the geometry trial, change one supported
   choice at a time where observations identify a persistent error. Include
   parent convection, cloud/PBL behavior, and forcing disagreement. Keep source
   comparisons at matched lead/valid times with availability documented. Snow
   assimilation, new PBL options, and land cycling remain separate experiments.
7. **Operations:** benchmark the exact accepted layout, physics, native output,
   and publication path; then run full horizons in shadow operation. Count data
   delays, failures, stale output, and maintenance effort alongside core-hours.

Use two cases per relevant regime for development: diurnal valley flow, strong
synoptic flow, cloudy/marginal convection, and snow transition. Reserve at least
one additional episode per regime for an untouched pilot evaluation. This is a
small engineering screening set, not evidence of seasonal skill. Add prospective
cycles and seasonal coverage before claiming broader reliability. Case IDs and
regional coordinates belong in `config/`; evaluation stays in the separate L3
scorecard package described by the architecture.

### Verification and promotion rules

- Compare with the current regional forecast and available direct-source
  forecasts. Retain issued forecasts, source cycles, observations, and matching
  code so each result is reproducible. Verify lead ranges separately.
- Use common observation masks and valid times. Record station elevation,
  exposure, instrument height, and sampling interval. Preserve point and common-
  footprint comparisons; use neighborhood scores for gridded cloud/snow/rain
  observations where spatial coverage supports them.
- Score wind-vector error, speed bias/MAE, shear, and valley-flow reversal/onset;
  temperature bias/MAE and cloud/precipitation timing; snow cover, depth, and SWE
  independently. Include misses and false alarms for predeclared event thresholds.
  Temperature and radiation/flux diagnostics help identify the cause of snow errors.
- Use flight tracks as selectively sampled evidence. Account for aircraft sink,
  sensor quality, pilot sampling, and thermal versus ridge/wave lift. Unflown
  cells remain unobserved. Compute soaring-specific scores downstream; raw WRF
  vertical motion and experienced climb rate have different physical meanings.
- Predeclare primary metrics, tolerated regressions, event thresholds, and cost
  limits before looking at held-out results. Test score formulas independently,
  report sample counts and uncertainty by independent weather episode, and retain
  per-regime results. Thousands of adjacent track points do not create thousands
  of independent weather cases. Insufficient evidence leaves a product experimental.
- Promote only after numerical gates, held-out weather comparisons, and delivery
  budgets pass. Require improvement in the declared target phenomenon, explicit
  non-regression checks for the other critical fields, and positive briefing-time
  margin. Measure availability and deadline success over prospective cycles; retain
  baseline configuration and a reproducible rollback path.

Defer moving/two-way nests, LES/sub-kilometre physics, automatic site clustering,
arbitrary namelist overrides, offline `ndown` parent reuse, differing nested run
horizons, and automatic grid blending until a measured need justifies them.
The initial configuration describes fixed domains for a common integration window.

[nest-wps]: https://www2.mmm.ucar.edu/wrf/site/namelist.wps_best_practices.html
[nest-wrf]: https://github.com/wrf-model/WRF/blob/v4.8.0/run/README.namelist
[nest-adaptive]: https://github.com/wrf-model/WRF/blob/v4.8.0/dyn_em/adapt_timestep_em.F#L338-L349

## 6. Feedback follow-through

The [2026-11-13 disposition](review-remediation.md#feedback-disposition-2026-11-13)
records every supplied recommendation. Complete these bounded follow-ups alongside
the phases above; retain the archived baseline and its original cadence.

1. **Consumer velocity:** use the published pressure, temperature and specific
   humidity with WRF constants to invert `omega_pas`. Verify both signs and
   rounded wire inputs against native destaggered W in the consumer suite.
   Document the encoded proxy separately from material pressure tendency.
2. **Hourly delivery:** measure full 72-lead post, verification, storage, transfer
   and consumer latency on deployment hardware. Retain hard briefing deadlines
   and measure positive margin over prospective cycles. A 48-hour alternative
   requires evidence that users accept the shorter horizon.
3. **Vertical headroom:** qualify the measured paired lower/upper-stretch candidate
   on warm/cold full horizons. Record minimum BL count, first-level clearance,
   exported top, largest gap, CAPE and observation scores. Preserve all existing
   admission bounds; promote only with headroom and acceptable skill/top coverage.
4. **Solver cost:** compare fixed/adaptive steps and 6/12 CPU layouts on identical
   cases, then test a geometrically valid parent-halo reduction separately.
   Check common-interior winds, pressure, clouds and boundary sensitivity before
   combining changes. Report core-hours and wall time; cell counts are estimates.
5. **Source operations:** connect inventory-drift classification to real alerting,
   with provider-change fixtures and retained last-good forecast handling. Add
   a native `use_sh_qv` sensitivity fixture when qualifying the live IFS path.
6. **Distribution:** measure destination verification and object-request costs
   before proposing hash-bound validation reuse or larger temporal shards. Require
   corruption/recovery tests and consumer range-read qualification for any change.
