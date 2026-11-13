# Review remediation

This page records implemented review decisions and remaining qualification.
The [project plan](plan.md) owns release gates; [review evidence](review-plan.md)
owns the paired pilot, albedo replay, and publication benchmarks.

## Implemented foundation

- **Native admission and recovery.** Attempts use private scratch and serialized
  execution. Solver receipts bind forcing, active-domain geography, native outputs,
  and stage wall times. Recovery verifies completed output before reuse. Image
  checks cover dependency imports, native namelist parsing, ICON conversion,
  and synthetic single-domain and nested WPS/WRF execution.
- **Publication.** Fresh promotion decodes every required shard and checks native
  column gates. Recovery verifies committed hashes and canonical metadata.
  New ready receipts bind the diagnostics digest through pruning and notification.
  Legacy receipts remain readable; recovery requires a retained trusted manifest,
  and standalone notification requires the bound digest.
  Local writes retain data and ancestor-directory sync; promotion installs a
  complete sibling tree under the cycle lock. S3 uses immutable attempt payloads,
  conditional ready creation, and conditional latest updates. Pruning and post
  share an attempt lock. Remote service qualification remains below.
- **Forcing.** GFS and IFS support indexed byte-range acquisition with snapshot
  checks. NOAA's paced regional filter remains optional. ICON validates donors,
  reuses grid weights and static fields, and can write direct WPS v5 intermediates.
  Each recipe verifies its time, grid, vertical and soil inventory before solve.
  Candidate profiles identify comparison inputs; scheduled runs use the primary.
- **Nesting.** Resolved locks freeze one-way parent buffers for coarse GFS/IFS
  forcing. WPS, geography, initialization and receipts cover active domains;
  published coordinates describe the fine domain. ICON can retain the same fine
  coordinates without a parent. Nested skill needs observation scoring;
  the [expanded run](review-plan.md#expanded-full-horizon-run-2026-11-13) records cost.
- **Science.** The pinned Noah-MP albedo guard, native radiation checks, AFWA
  MUCAPE admission, Shuell MSLP fallback, WRF constants, humidity conversion,
  rainfall windows, hybrid coordinate and wind rotation are implemented.
  Coupled MYNN output uses the current total `CLDFRA_BL`; `CLDFRA` is the older
  radiation snapshot. Lake, mixing and drag arrays carry per-domain values.
  Native face ordering is checked before midpoint conversion. Audit and admission
  apply albedo validity to daylight cells while checking fluxes at every time.
  Sampled Courant diagnostics use saved `DT`, face thickness and Lambert scale.
  Unpopulated timestep diagnostics were removed; legacy receipts remain readable.
- **Cost and scope.** Thompson tables are prebaked and checked with read-only
  reuse. The solver defaults to a portable ISA with explicit optimized targets.
  CI persists native BuildKit layers and cancels superseded PR jobs. Base images
  and action revisions are pinned. Encoding and uploads have bounded parallelism;
  post releases full volumes before reading the next lead. Shards encode exactly
  one time step. Parcel diagnostics remain in the consumer.
- **Configuration.** Typed profiles own optional defaults. Individual checks and
  planning reject duplicate model namespaces and mismatched preloaded identities.
  The geography bundle digest owns lock identity; the unused digest list was removed.
  Prep emits the history sidecar referenced by its integration namelist.

## Archived science evidence (2026-11)

These experiments used the archived profiles and geometry. They establish
specific numerical behavior; current-profile operational qualification remains
separate. [Science conventions](science.md) records the physical assumptions.

- **Radiation.** The patched pair replayed identical native inputs and removed
  all negative shortwave samples. The three-minute dawn replay checked all
  21 saved frames. Preserve the full [albedo replay](review-plan.md#albedo-replay)
  and its native audit command when comparing future solver builds.
- **CAPE.** Five positive cold ICON cells were below 0.4 J/kg. The pinned AFWA
  routine returned 2,871 J/kg for an ideal convective sounding. A July GFS hour
  produced positive MUCAPE in 13,777 of 26,640 published columns, peaking at
  462 J/kg; Zarr retained every positive value. Sounding skill remains unscored.
- **Vertical sensitivity.** A uniform 4% reference-column stretch reduced the
  original minimum count below 2 km from 17 to 16, failing 861 production cells,
  including 530 land cells. Lower stretch values 1.10 and 1.09 passed that stress
  at the cost of lower exported tops. These are artificial sensitivity checks.
- **Native vertical cases.** Cold initialization with lower stretch 1.11, 1.10,
  and 1.09 passed all 26,640 columns, with minimum counts 17/18/18 and exported
  tops 7.54/6.76/5.84 km AGL. The lowest midpoint was 21.49 m in all three.
  July initialization at up to 33.0 °C also passed: counts 17/17/18 and tops
  8.29/7.47/6.48 km. Lower stretch 1.09 retained 18 levels and a 6.49 km top
  after one hour. Pairing lower 1.09 with upper 1.08 retained 18 levels in cold
  and warm initialization, with minimum tops 7.44/8.20 km. In July the highest
  17th level moved from 1,973 to 1,788 m. After an hour it retained 18 levels
  and an 8.21 km top, 97 m below the archived shipped setting. MUCAPE mean
  absolute difference was 0.57 J/kg; six cells exceeded 100 J/kg and one reached
  268 J/kg. Full-horizon comparison is required before selecting a new grid.
- **Terrain.** The archived production WPS maximum was 4,203 m, below the
  5,137 m subgrid summit checkpoint. Lowest-level clearance therefore needs both
  initialized-grid checks and downstream terrain qualification.
- **Hydrology.** Noah-MP runoff options 1/1 select TOPMODEL groundwater; the
  upstream 3/3 defaults use free drainage. Cold initialization sets a 2.5 m water
  table and 4,900 mm aquifer storage. Both 22-hour pilots retained a median
  2.5 m water table. The first July hour had land medians of 236 W/m² sensible
  heat, 88 W/m² latent heat and 1,057 m PBL depth; water-table change was at most
  0.6 mm. A matched 3/3 versus 1/1 hour changed land mean absolute sensible heat,
  latent heat and PBL depth by 0.06 W/m², 0.03 W/m² and 0.11 m; the largest PBL
  change was 70 m. Warm land-state cycling and longer wet/dry cases remain open.
  [WRF options](https://github.com/wrf-model/WRF/blob/v4.8.0/run/README.namelist)
  · [Cold initialization](https://github.com/NCAR/noahmp/blob/5da0b241e48ecfd9a2a1bd667ed554765856d589/drivers/wrf/NoahmpInitMainMod.F90#L164-L167)
- **Gust convention.** Noah-MP supplies sensible heat from actual temperatures,
  supporting the existing gust buoyancy algebra. Replacing `LH/Lv` with native
  vapor flux `QFX` changed archived gusts by under 0.001 m/s at p99 and at most
  0.072 m/s. Frozen cases need separate evaluation because sublimation changes
  the energy-to-mass conversion. The exported gust is an uncalibrated endpoint
  estimate; published surface fluxes are interval means.
  [Noah-MP sensible heat](https://github.com/NCAR/noahmp/blob/5da0b241e48ecfd9a2a1bd667ed554765856d589/src/SurfaceEnergyFluxBareGroundMod.F90#L150-L154)
  · [Vapor flux](https://github.com/NCAR/noahmp/blob/5da0b241e48ecfd9a2a1bd667ed554765856d589/drivers/wrf/WaterVarOutTransferMod.F90#L62-L65)
- **MSLP.** The retired surface-based fallback differed from native AFWA by
  0.96–1.02 hPa mean absolute and up to 11.52 hPa. The implemented lowest-level
  Shuell fallback agreed within 0.2 Pa mean absolute and 4.5 Pa maximum after
  storage rounding across both corrected pilots. Positive native values remain
  authoritative; absent or nonpositive cells use the fallback.
  [AFWA reduction](https://github.com/wrf-model/WRF/blob/v4.8.0/phys/module_diag_functions.F#L911-L962)
- **Cloud snapshot correction.** Reprocessing archived native frames with current
  MYNN cloud selection removed total-cloud excesses up to 52.08 percentage points
  in GFS and 23.57 points in ICON. Both exported outputs matched quantized current
  `CLDFRA_BL` overlap exactly. This validates snapshot selection; cloud skill
  still requires observations.
  [WRF snapshot copy](https://github.com/wrf-model/WRF/blob/v4.8.0/phys/module_radiation_driver.F#L1403-L1416)

## Archived forcing and cost evidence (2026-11)

- **GFS inventory.** WPS retained 33 isobars plus the surface, excluding eight
  isobars below its 100 Pa `pmin`; the metgrid total was 34. The historical
  regional-filter set held 55 files and 59 MB. Acquiring 53 missing files took
  about 24 minutes under NOAA pacing; manifest sealing and re-verification each
  took about 11 s. This timing predates the default indexed acquisition path.
- **ICON upper air.** The production cutout matched 15,794 DWD regular-grid
  points at f000. At levels 55/74, temperature mean absolute differences were
  0.04/0.17 K, wind-component differences 0.07–0.18 m/s and humidity differences
  0.006/0.034 g/kg. Pressure differences 0.99/4.14 hPa fell to 0.08/0.19 hPa
  at common geometric heights. Across 26,640 initialized columns, GFS/ICON
  temperature MAE at 250/1,000/2,000 m AGL was 1.38/0.38/0.34 K and vector wind
  differences were 1.29/1.97/1.58 m/s. This measures source disagreement.
- **Acquisition admission.** The historical 475-object ICON f000 cache check
  took 0.84 s with eight workers, including reads. Subsequent remapping fixes
  use float64 seam geometry, normalized weights and positive-land soil donors.
  The 2026-11-12 optimization reuses cropped invariants and binds admission to
  exact source hashes. An archived three-hour replay took 14.77 → 13.56 s in one
  trial, with 1,417 → 1,267 value decodes and about 4.2 MiB of invariant storage.
  WPS outputs and provenance inventories were byte-identical.
- **SLA estimates.** Historical pilot extrapolations gave 5.35 and 6.06 hours
  against a 5.25-hour delivery budget. Identical ICON pilot solves took 24 and
  45 minutes under different host load. These estimates predate current nesting
  and cannot establish its SLA. The short native and publication measurements
  remain in [measured tradeoffs](review-plan.md#measured-tradeoffs).
- **Table reuse and diagnostics.** Five archived runs produced byte-identical
  374 MB Thompson tables, motivating current prebaking. Area weighting changed
  hourly pilot flux means by under 0.0005 W/m²; diagnostics retain grid-cell means.
- **Sea/lake geography.** Official MODIS tiles classify two regional seas as
  lakes; archived initialization propagated their default 10 m depths into the
  lake model. Configured seeds now select complete water bodies for external
  water temperatures, including elevated coastal cells. The expanded-grid check
  changed 4,081 parent and 2,142 child cells while preserving terrain, water
  coverage, total water fractions and inland lakes. Initialization admission
  checks the sealed lake mask before integration. Thirty-two regressions cover
  topology, malformed geography, immutable admission and initialization failures.
  [NCAR guidance](https://forum.mmm.ucar.edu/threads/how-to-change-land-use-category-for-black-sea.10277/)

## Remaining qualification

Evidence is scoped to source, grid/nesting, physics, architecture and weather regime.
Current ARM64 synthetic tests cover single/nested MYNN, lake physics and adaptive
steps. Archived GFS/ICON real cases cover their original profile hashes. IFS has
acquisition/normalization tests; a live-format native run remains open. The expanded
nested GFS cycle passed full-horizon physical gates and missed latest promotion by
39 seconds. Observation skill, winter ice coverage and reliable delivery margin
remain unqualified.

Archive each qualification with the exact config and producer snapshots, OCI/build
manifest, geography and forcing inventories, namelists/sidecars, native logs,
run/solver/ready receipts, diagnostics, audit version and hardware. Record wall time,
peak memory, core-hours, scratch usage and transfer bytes beside the price basis.
These immutable identities carry qualification across profile revisions.

1. **Consumer and remote publication.** Complete a real Zarr provider, shared
   receipt/identity agreement, authenticated trigger replay and served tile,
   sample and profile checks. Read across inner tiles and reject corrupt ranges
   and mismatched receipts. Exercise private R2 with competing attempts, lost
   commit responses, interrupted transfers, and consumer range reads. Local and
   stubbed S3 tests establish application behavior; live service behavior needs
   this deployment test.
   [R2 conditional operations](https://developers.cloudflare.com/r2/api/s3/conditional-operations/)
2. **Current full-horizon delivery.** Add delivery margin to the measured producer
   run and time consumer work on deployment hardware. Repeat enough cycles to
   measure arrival variability and retry
   margin. Test mount permissions for the deployment UID and bind both runtime
   and solver image digests when deploying separate images.
3. **Observation scorecard.** Score cold and warm cycles, lowlands and high
   terrain, snow/lake cases, shallow cloud, surface fluxes, CAPE and gusts.
   Compare source, vertical grid, land initialization and physics changes
   separately. The 9 km parent's convection choice also needs a controlled
   comparison. Publication gates check physical admissibility.
4. **Vertical references and velocity.** Qualify native/subgrid terrain datum
   conversion and align the consumer inverse with the producer's moist-density
   encoding. A direct geometric-velocity field requires a coordinated wire change
   only if consumer design warrants it.
5. **Long integrations and new donors.** Measure accumulation rounding in warm
   long forecasts against higher-precision sums; evaluate radiation interpolation
   separately. Qualify actual ice coverage before accepting ice-bearing donors.
   Observe complete source inventories over multiple cycles before changing
   primary forcing or dissemination allowances.

## Feedback disposition (2026-11-13)

Reviewed the supplied comprehensive report against the pinned WRF source,
current callers, and retained forecast bytes. Original attachment SHA-256:
`b86d9501467209bbc4ca6da6447ae742085b80ad141a09166fec61994d603041`.
The retained local `output/forecast-20261113/evidence/review-feedback-audit.json`
contains input hashes, source links and reproduction code; SHA-256:
`7b81813fdd3e047e29eb37c00f98bb48782539f2722f059f8780a09c5aeae896`.
This section owns each decision; the [follow-up plan](plan.md#6-feedback-follow-through)
owns remaining work. Numerical conventions and upstream links live in
[science notes](science.md); operator sources live in [review evidence](review-plan.md#external-nesting-evidence-researched-2026-11-13).

- **AFWA cardinality: implemented.** Emit both options per active domain and
  guard emitted namelist shapes against the pinned Registry. The reported P0
  omitted WRF's subsequent all-domain normalization. The archived d02 contains
  finite, unmasked `AFWA_CAPE_MU` at the first, middle and final histories, with
  maxima 0, 10.025 and 101.273 J/kg. Its successful publication is consistent
  with that source behavior. Calling AFWA options scalar/global was also
  imprecise: their Registry shape is per-domain; effective activation is global
  in this solver. Rechecking retained bytes resolves the alleged contradiction
  without another full integration.
- **Nested configuration admission: implemented.** `validate_bundle` compiles
  the actual single/nested layout. The Registry regression covers emitted keys
  and supported conditional branches; source revisions require snapshot review.
  Live post continues to require the selected domain's native MUCAPE. Reuse the
  existing missing-MUCAPE reader regression; its error already names the file
  and `afwa_buoy_opt`. Source normalization and retained d02 output supply the
  native nesting evidence beyond a synthetic fixture.
- **Vertical velocity: corrected diagnosis.** The matching moist inverse
  recovers geometric velocity algebraically, including wave motion. Across
  1,419,840 samples in the first published frame, maximum absolute error against
  native destaggered W was 0.00007685 m/s using rounded published inputs.
  Regression tests cover signed/zero velocity, moisture, wire rounding and the
  existing consumer dry-inverse bias. Aligning that consumer inverse remains
  open; an emergency schema v4 or extra field has no demonstrated need.
- **Pressure and humidity: retained.** `P + PB` is total thermodynamic pressure,
  so the proposed vapor-pressure correction would introduce an error.
  `use_sh_qv` controls later interpolation/reconstruction; GFS supplies RH.
  Removing this option would change IFS/ICON initialization.
- **Vertical headroom: qualify.** Keep the configured BL-level floor and hard
  column gates. The 861 failures came from an artificial 4% column stretch.
  Lower stretching changes BL density, exported top and meteorology. Test the
  measured lower/upper-stretch pair across full horizons before promotion;
  automatically setting 1.09, accepting 16 levels or making failures warnings
  would discard the existing quality requirement.
- **Hourly tail: implemented for future runs.** The long profile now emits all
  72 hourly leads. The archived schedule has 48 leads: 36 hourly plus 12 tail
  steps. This adds 24 frames, or 50% temporal output, using existing hourly native
  histories. Post, storage and consumer cost increase; solver horizon stays fixed.
  Preserve the requested 72-hour horizon. A shorter product needs usage evidence.
- **Parent halo: retain pending experiment.** The actual parent has 134 × 124
  mass cells; the child has 222 × 192. At a 3:1 time-step ratio the parent accounts
  for about 11.5% of ideal cell-step work. Halos 10/12 fail the current minimum
  grid dimensions. Halo 18 is geometrically admitted and saves about 3.9% ideal
  work; meteorological adequacy and real runtime remain unmeasured.
  [WPS placement and timestep guidance](https://www2.mmm.ucar.edu/wrf/users/namelist_best_prac_wps.html).
- **Adaptive stepping: retain pending experiment.** NCAR recommends child
  `max_step_increase_pct = 51` to permit nested step adjustment. The value alone
  does not demonstrate timestep churn. Compare fixed/adaptive steps and MPI
  layouts separately; the report's 15–25% savings remain unsupported. The saved
  78-hour integration used 16,615 child steps, about 0.6% above an ideal constant
  17-second run. Constant 15 seconds would require about 12.7% more steps;
  these counts alone cannot predict wall time.
  [NCAR adaptive stepping](https://www2.mmm.ucar.edu/wrf/site/documentation/users_guide/running_wrf.html#adaptive-time-stepping).
- **Provider inventory: implemented.** Recipes own supported provider
  level identities; profiles declare expected metgrid dimensions including the
  surface. Derive duplicated internal counts from recipe inventories and reject
  incompatible profiles early. Retain serialized fields needed for lock replay.
  An arbitrary count cannot specify a replacement provider vertical coordinate.
- **Cloud I/O: implemented.** Read one effective full-column cloud
  field and carry one cloud slab. Full P/PB remain reachable for pressure ordering
  and low-cloud selection. Current MYNN `CLDFRA_BL` is total cloud; clipping it
  to exported levels would lose upper cloud. Preserve strict live selection and
  sparse-archive fallback.
- **Operational errors: planned with a caller.** Keep precise inventory errors
  and fail-closed admission. Add structured drift classification with the actual
  alert/monitoring integration and migration fixtures; an unused exception class
  would enlarge the API without improving delivery.
- **Module size: simplify where touched.** Cloud ownership removes duplicate
  state. Existing file/function budgets guard growth. Split at a demonstrated
  boundary when needed; no speculative baseline/step module split.
- **Publication and sharding: retain integrity.** Verify every destination's
  payload. Reuse validation only with measured mirror cost and identity-bound
  destination verification. Keep one-time-step shards; larger temporal shards
  need consumer range-read and cost evidence plus a coordinated contract change.
- **Competitor conclusions: use corrected evidence.** CanadaRASP publishes its
  processing source and moved from WRF to direct model output. SoaringMeteo's
  historical solver guide and newer Scala processing code describe different
  systems. The report's grades and comparative superiority claims have no matched
  benchmark. Retain the producer/consumer split, bounded streaming, immutable
  provenance and independent oracles; observation skill remains unqualified.
- **Positive physics audit: retained as corroboration.** Constants, coordinate
  construction, T/THM, MSLP, gust, saturation, cloud overlap, accumulation windows,
  wind rotation, CFL, ICON normalization, WPS layout, GFS inventory and Zarr codec
  already have focused checks. The reported schedule margin is superseded by
  the measured 39-second latest-promotion miss. New work preserves oracle
  tolerances and the [remaining qualification](#remaining-qualification).

## Infrastructure decisions

Boto3/botocore owns S3 signing, credentials, pools and retries. HTTPX and Tenacity
own shared HTTP transport and bounded transient retries. Source adapters retain
provider index parsing, coherent byte-range snapshots and weather admission.
Selected-message SHA-256 values identify downloaded bytes. An official provider
client can replace an adapter when it preserves those guarantees with a smaller
reachable surface.

The bounded WPS v5 codec and strict ZEP-0002 checks remain producer boundaries.
Numcodecs owns compression; publication verifies the complete shard inventory.
Independent staging and published copies preserve replay: measured APFS
copy-on-write took 2.92 s versus 2.61 s for independent writes. Revisit backend
copying only with equivalent digest and recovery behavior. Extend spatial
sharding with the consumer when a real grid exceeds the wire budget.
