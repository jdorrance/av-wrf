# Review remediation

This plan consolidates all 42 numbered findings from the three reviews against
`bb221b0`. A is the read-only review beginning with the ICON image extra.
B is the cross-repo review beginning with the producer-consumer split.
C is the review beginning with late publication. IDs preserve the source
priorities; related findings share an entry. D is the follow-up science
review below. The [project plan](plan.md) owns release gates.

## Execution order

1. Backport the upstream Noah-MP albedo guard. Rebuild the pinned image and
   replay both full pilots with identical geography, inputs, ranks, and leads.
   Compare native radiation and published output before and after the patch.
2. Exercise recovery after native failure, interruption before receipt write,
   publication corruption, late completion, and competing destinations.
3. Verify forcing admission, image dependencies, grid and schedule policy,
   and the separate real and WRF namelists.
4. Re-run repository checks and record remaining release dependencies.

## Verification

- Both full pilots passed the radiation and publication gates. See
  [paired evidence](review-plan.md#albedo-replay) for the matched inputs,
  saved-frame checks, and remaining local disagreements.
- `uv run verify` passed all 83 tests and the lint, typing, schema, and
  architecture gates. `git diff HEAD --check` covered the full change.
- The offline image test exercises real ecCodes re-encoding across a UTC
  date rollover, coastal surface weights, invalid mirror responses, and
  WPS ungrib. The archived host reconstruction reproduced the normalized
  ICON files and their acquisition inventory byte for byte.
- The hardened ARM64 image cold-started two ICON leads offline as UID
  65534. All 80 Zarr object hashes matched, and solve and pruned publish
  retries passed. The WPS floating-point status note also appears in
  the old build; the intermediates match and checked fields are finite.
- Recovery tests cover failed native execution, interruption before a
  solver receipt is stored, an incomplete published receipt, corrupt Zarr,
  producer drift, malformed latest pointers, and competing destinations.

## Changes in this pass

- **A P0-1 · ICON image.** Install the ICON extra in the solver image and
  smoke-test acquisition dependencies. The previous local pilot acquired on
  the host, then solved in Docker, so that run did not test in-image acquisition.
- **A P0-2, A P1-6 · Native coverage.** Test real ecCodes re-encoding and
  validate point count, validity, packing, and values. The offline image
  smoke exercises the normalized records through WPS ungrib. Local native
  pilots cover metgrid, real, WRF, and NetCDF. A full WRF CI case remains
  a coverage gap.
- **A P0-3 · Solver recovery.** Serialize each attempt, solve in private
  scratch, and install completed output with a local receipt for crash
  recovery. Keep earlier scratch for diagnosis. The path equality check
  detects symlinked descendants even when the work root is canonical.
- **A P0-4, A P0-5 · Publication recovery.** Validate committed receipt
  identity against the lock and the live or fixture namespace. Derive the
  Zarr keys from the profile and leads, rehash each object, and check
  canonical metadata and coordinate bytes before advancing latest or
  pruning staging. Validate diagnostics identity; the current ready
  receipt carries no diagnostics digest after pruning. Fresh live
  promotion requires the running producer SHA to match the lock.
  Committed recovery verifies archived bytes across deployments.
- **A P1-1, A P1-2 · ICON weights and cache admission.** Check finite
  weights and encoded values. Before cache commit, unpack each new
  donor and verify its cycle, valid time, process, time template,
  selector, and level. Try the next mirror on rejection. Assembly checks
  the common mesh and vertical ordering. A corrupt legacy cache entry
  names the object to remove. The 475-object f000 inventory check took
  0.84 s with eight workers, including reads.
- **A P1-4 · Grid spacing.** Require positive finite spacing in the domain
  validator as well as the YAML parser.
- **A P1-5, B P2-11 · Horizon and prep.** Use a single effective live age
  in validation and publication. Check January and July briefing margins
  in product preflight and the exact date when planning. Emit a separate
  real template with the full boundary bracket. Validate the model,
  cycle, and ready key before comparing latest timestamps in UTC.
- **B P2-8 · GRIB and process bounds.** Validate latitude scan metadata
  against endpoints and unpack GFS data to check missing values. Local
  native deadlines are opt-in with `AV_WRF_NATIVE_TIMEOUT_S`; the default
  has no deadline. Remote schedulers own their job limits and cancellation.
- **B P1-7 · Native decoder.** Remove the legacy JasPer build and unused
  PNG codec. Retain WPS's built-in GRIB2 templates. The Linux ARM64
  decoder produced byte-identical WPS intermediates for all 46 archived
  inputs and the two complex packing controls. JPEG2000 and PNG inputs
  now exit with a packing error.
- **C P1-1 · Late publication.** Report each destination's live pointer
  outcome alongside archival publication. Webhook reconciliation remains
  a separate retryable command.
- **A P3-1 · Dead surface.** Remove unused scalar and 2D staggered
  types. Keep the documented verification commands, source profile keys
  used by archived locks, and output controls validated by shard codecs.

## Claims already covered or ruled out

- **A P1-3, B P0-2 · MPI floor and terrain shadows.** The pinned WRF
  check divides `e_we` and `e_sn` by the MPI mesh dimensions. Plan
  applies that rule and rejects an oversized mesh. WRF also iterates
  terrain shadow propagation across patches with `HALO_TOPOSHAD`.
  [MPI guard](https://github.com/wrf-model/WRF/blob/v4.8.0/share/module_check_a_mundo.F#L2773-L2799)
  · [Terrain shadows](https://github.com/wrf-model/WRF/blob/v4.8.0/phys/module_radiation_driver.F#L3395-L3461)
- **B P1-3 · Interrupted promotion.** Promotion streams each object
  after it verifies. A late failure can leave verified objects without
  `_ready.json`. The owner binds the attempt and post-manifest SHA-256;
  a retry reuses matching objects or clears partial output when the
  manifest changes. Changing immutable staged bytes requires a new
  attempt or explicit cleanup. `_ready.json` is the commit boundary.
- **B P1-4, B P1-5 · Store lock and pruning.** Stable locks live outside
  the deletable data tree. Prefix deletion syncs the parent. Pruning
  removes the post manifest and staged bytes together under the attempt
  lock. A verified ready cycle can be recovered after pruning.
- **B P1-6, A P2-4 · Native columns and bounds.** Post and publish require
  pressure below surface pressure, physical temperature and pressure,
  monotonic heights, lowest-level clearance, and valid vertical spacing.
  Real uses the configured spacing ceiling. The surface humidity cap
  covers low pressure. Cloud fraction and friction velocity are bounded.
- **B P1-7 · Image pins and CI.** WRF and WPS commits are pinned and
  recorded alongside patch digests. CI observes producer and build
  changes. Archived manifests still parse their historical JasPer pin.
- **B P2-8 · Existing GRIB checks.** The ecCodes command has a timeout.
  GFS rejects reversed i scanning and missing required values, with
  explicit masked soil and snow exceptions. Process IDs are part of
  the admitted NOAA feed identity; qualify a new ID before accepting it.
- **B P2-10 · Grid geometry.** The producer checks longitude bounds,
  a nonzero cone, full-grid registration, and rotation. Unsupported
  projections fail at admission. The writer still uses one spatial shard
  per frame, capped at 64 inner tiles (512 × 512 cells for a square).
- **B P3-12 · Static checks.** The checker covers qualified dataclasses,
  `builtins.open`, `typing.Any`, and `abc.ABC`. The image digest binds
  its time-zone data. A wheel pin alone would still permit host
  `zoneinfo` data to take precedence.
- **B P3-13 · Producer lock.** The run lock detects local source drift.
  `manifest.lock` pins vendor, oracle, and patch files separately.
  Pin deployment by digest to bind its installed dependency environment.
  The CLI records the supplied solver OCI digest; it does not attest the
  running image. The broad source scope preserves the code identity.
- **C P2-6 · Accumulation windows.** Tail rates and fluxes use the declared
  interval and publish the real start and end. Gust is an instantaneous
  endpoint field. Consumer integration must preserve that distinction.
- **C P2-7 · Gust interpretation.** The referenced IFS stable-air formula
  keeps the neutral turbulent coefficient. The buoyancy input is
  instantaneous and the published surface flux is a window mean.
  This WRF adaptation uses native boundary-layer depth and remains an
  uncalibrated endpoint gust estimate.
  [IFS physical processes](https://www.ecmwf.int/sites/default/files/elibrary/2016/79989-ifs-documentation-cy43r1-part-iv-physical-processes_1.pdf)
- **C P3-12 · Webhook destination.** HTTPS, redirect rejection, URL
  sanitization, and bearer authentication already exist. The operator
  selects the destination and supplies its credential. Private HTTPS
  endpoints support self-hosted deployments.

## Remaining decisions and qualification

- **A P2-1, B P0-1 · Consumer contract.** The adjacent consumer has a
  draft Zarr schema and only registers GRIB readers. It still needs a
  Zarr provider, trigger integration, shared ready/latest/diagnostic
  schemas, and a versioned producer identity. First read an archived
  producer cycle offline across two inner tiles and reject a corrupt
  range or mismatched receipt. Then deliver and replay one authenticated
  ready event in staging and verify a served tile, sample, and profile.
  Producer HTTPS authentication and error redaction are already present.
- **B P3-13 · Split-image provenance.** Post and publish verify source
  identity. A separate runtime image has its own dependencies and digest.
  Bind that digest alongside the solver digest when qualifying a split
  deployment and its shared receipts.
- **A P2-2 · Same-store copies.** Keep independent staged and published
  copies. Copy-on-write took 2.92 s versus 2.61 s for independent writes.
  Revisit with a backend whose copy preserves digest validation and
  retry behavior.
- **A P2-3, C P2-11 · Remote fencing.** Local filesystem publication is
  supported on one kernel. For R2, isolate each attempt's payloads and
  use a conditional ready commit and latest update. The v1 ready receipt
  fixes `forecast.zarr` under the cycle prefix; attempt-scoped paths need
  a consumer contract change or a single publisher with exclusive writes.
  Exercise racing attempts, a lost commit response, interrupted promotion,
  and range reads before enabling a remote backend. Read the latest
  pointer without caching; cache immutable forecast bytes.
  [R2 conditional operations](https://developers.cloudflare.com/r2/api/s3/conditional-operations/)
- **B P2-9 · Accumulation precision and radiation interpolation.**
  WRF's optional `bucket_J` covers radiation, which is outside the
  published accumulation fields. At 2 J/m² spacing, rounding one
  addition per 15 s step contributes up to 0.067 W/m² of mean flux
  error with either sign. Measure the actual long warm run against
  a higher-precision accumulator. Evaluate `swint_opt` separately.
- **B P1-7 · Runtime user.** The solver and post images default to root.
  The nonroot image smoke and two-lead end-to-end run pass. Configure
  the deployment's mount owner and permissions before changing image
  defaults.
- **B P2-10 · Larger domains.** Introduce multiple spatial shards with
  the consumer when a grid exceeds the current tile budget. Compare
  cross-shard reads against a full-frame decode before enabling one.
- **C P1-2 · Donor revisions.** The measured DWD timestamps were object
  modification times. They did not establish repeated rewrites of the
  same key. Preserve reproducible cached bytes and use a fresh cache
  for explicit source re-acquisition.
- **C P1-3 · Vertical velocity.** The existing wire field is documented
  as a moist hydrostatic conversion of geometric velocity. Qualify the
  consumer inverse or version the wire field for geometric velocity.
- **C P1-4, C P2-10 · Land spin-up and shallow cloud.** Qualify a warm
  land-state run and a shallow-cloud physics alternative against
  observations before changing the operational physics. Publication
  checks physical admissibility; it has no observation gate.
- **C P1-5 · Ice-bearing donors.** Current admission fails closed because
  ice thickness alone cannot give fractional coverage. The public feed
  lists thickness and lake fraction. Its ice-fraction endpoint returned
  HTTP 404. The pilot uses lake donors around Lake Sevan, which freezes
  irregularly. Qualify a lake-only conversion or a source with actual
  ice fraction before accepting icy donors.
  [DWD feed](https://opendata.dwd.de/weather/nwp/v1/m/icon-eu/p/)
  · [Lake Sevan observations](https://doi.org/10.1029/2023WR036511)
- **C P2-8 · Lowest-level margin.** The native initialized grid and
  published columns are checked. Measure the clearance on warmer
  high-terrain cases before selecting a different vertical grid.
- **C P2-9 · Late arrivals.** Measure full inventories and runtime across
  cycles. The full product allows 27 minutes between its stated p95
  GFS arrival and planned daily trigger. Alternate launch schedules require
  product and consumer qualification. The publisher now distinguishes
  a late archived cycle.
- **C P3-13 · Height datum.** Producer AGL uses a common native datum.
  Qualify datum conversion with the downstream Copernicus terrain before
  mixing the two vertical references.
  [Datum details](science.md#1-physical-constants-wrf-inversion-vs-consumer-parcel-evaluation)

## D · Follow-up science tasks

1. **P1 · Test the vertical gates on the production domain (C P2-8).**
   The sea-level reference puts level 17 at 1,929.58 m AGL. A uniform
   4% stretch drops the count to 16. Raising the reference base
   temperature by 10 K gives 1,995.12 m and still passes; 12 K crosses
   2 km. The 1.09 stretch alternative gives 18 levels at sea level
   and 19 at 800 m. It also lowers the 40th level over high terrain.
   Production geogrid found 406 published land cells below 25 m MSL.
   With its exact terrain and an artificial 4% stretch, the current
   profile fails 861 cells, including 530 land cells. Both 1.10 and
   1.09 pass that stress;
   their minimum reference tops fall from 7.99 km to 7.20 and 6.24 km.
   Test warm lowland and cold high-terrain forcing with all three. Record
   minimum level-0 clearance, count below 2 km, maximum gap, and the
   exported top. Keep the existing wire gates. The old pilot reached
   a 20.95 m minimum over terrain no higher than 4,072 m. The 21.87 m
   reference summit clearance and 246 K crossing are sensitivity
   estimates at a fixed pressure profile. The highest production WPS
   terrain cell is 4,203 m; the actual summit is subgrid.
2. **P1 · Qualify runoff and land initialization (C P1-4).** The shipped
   Noah-MP options select TOPMODEL groundwater. The pinned WRF defaults
   are 3/3, with free drainage below the soil column. Cold starts set
   a 2.5 m water table and 4,900 mm aquifer storage by default. Both
   archived pilots retained a median 2.5 m water table after 22 h.
   The flux effect on a warm 48-hour forecast remains unmeasured.
   Compare 1/1 and 3/3 with the same inputs
   across dry and wet cases. Measure water-table/soil evolution,
   sensible and latent heat, boundary-layer depth, and observed cloud
   base. Test warm land state separately.
   [WRF namelist](https://github.com/wrf-model/WRF/blob/v4.8.0/run/README.namelist)
   · [Cold initialization](https://github.com/NCAR/noahmp/blob/5da0b241e48ecfd9a2a1bd667ed554765856d589/drivers/wrf/NoahmpInitMainMod.F90#L164-L167)
   · [Noah-MP groundwater evaluation](https://doi.org/10.1029/2010JD015140)
3. **P1 · Time the production grid (C P2-9).** The short pilot and a
   linear column-hour scale give 5.35 h. Scaling the archived full
   pilot gives 6.06 h on its host. Both are estimates against a 5.25 h
   deadline, before downstream work. Two identical ICON pilot solves
   took 24 and 45 minutes under different host loads. Run the full
   54 h on candidate hardware, record acquire/solve/publish/consumer
   durations, and measure enough cycles to set a retry margin. Track cold
   initialization separately. Five runs generated byte-identical Thompson
   tables (374 MB); measure the startup and transfer tradeoff before
   packaging or reusing them by solver digest.
4. **P2 · Compare forcing and nesting separately.** GFS's grid step is
   about 20.5–21.9 km east–west and 27.8 km north–south over the
   production domain. DWD's native grid is nominally 6.5 km; its
   normalized grid adds no resolved detail. The archived mesh remapped
   the 149 × 106 production forcing rectangle with all 75 height
   interfaces and no ice on the 23 available hours. Test same-cycle
   GFS and ICON-EU on the same production domain first. For nesting, try a
   one-way 9 km parent with a 3 km child using the native build. Test
   aligned dimensions, parent forcing coverage, domain-qualified
   geography and solver receipts, and child-only export with the same
   Zarr schema before adding configuration or changing the product.
   Peer workflows show precedent, not a quality result for this grid.
   [WPS nest guidance](https://www2.mmm.ucar.edu/wrf/site/documentation/namelist.wps_best_practices.html)
   · [Alpine nested design](https://soarwrf4.soaringmeteo.org/soarWRFEN.pdf)
   · [Dutch 1.2 km nest](https://gitlab.com/DavidRasp/blipmaps.nl/-/blob/master/rasp/NL1KMICON/namelist.wps)
   · [Dutch 4 km nest](https://gitlab.com/DavidRasp/blipmaps.nl/-/blob/master/rasp/NL4KMGFS/namelist.wps)
5. **P2 · Isolate damping and top-down mixing (C P2-10).** Vertical
   damping is conditional on the local Courant number; the pinned
   threshold is 1.0. YSU uses `ysu_topdown_pblmix` and the Shin-Hong
   switches only reach scheme 11. Compare each change separately,
   checking stability, vertical motion, and cloud/PBL observations.
   NCAR's 4 km example uses different physics and `diff_opt=1`.
   `km_opt=4` is horizontal Smagorinsky; correct the profile comment
   with the next profile revision because comments enter run identity.
   [NCAR example](https://github.com/wrf-model/WRF/blob/v4.8.0/test/em_real/namelist.input.4km)
   · [YSU dispatch](https://github.com/wrf-model/WRF/blob/06d4240ae989cc3e50af412bb472df3d9048783c/phys/module_pbl_driver.F#L1232-L1258)
   · [WRF damping](https://github.com/wrf-model/WRF/blob/06d4240ae989cc3e50af412bb472df3d9048783c/dyn_em/module_big_step_utilities_em.F#L2601-L2615)
6. **P3 · Gust temperature claim ruled out for the shipped land scheme.**
   The pinned Noah-MP bare and vegetated paths compute sensible heat
   using actual temperatures and transfer that flux to WRF `HFX`.
   The reported 0.38 m/s shift assumes a different input convention.
   Keep the endpoint gust algebra. Evaluate `QFX` for its moisture
   term: `LH` can include sublimation energy and a single vaporization
   constant does not recover the exact vapor mass flux. Using native
   `QFX` instead changes the archived gust by under 0.001 m/s at p99
   and at most 0.072 m/s. Qualify the change on frozen cases.
   [Noah-MP bare-ground flux](https://github.com/NCAR/noahmp/blob/5da0b241e48ecfd9a2a1bd667ed554765856d589/src/SurfaceEnergyFluxBareGroundMod.F90#L150-L154)
   · [Vapor flux transfer](https://github.com/NCAR/noahmp/blob/5da0b241e48ecfd9a2a1bd667ed554765856d589/drivers/wrf/WaterVarOutTransferMod.F90#L62-L65)
7. **P3 · Preserve native MSLP provenance.** Native AFWA reduces the
   lowest model level with the Shuell correction. The optional
   producer fallback uses surface diagnostics and a different plateau
   adjustment. Against the archived published leads it differs by
   0.96–1.02 hPa mean absolute and up to 11.52 hPa. All those leads
   had positive native MSLP; the unpublished baseline had zeros.
   Track fallback use on qualification cases before deciding whether
   live runs should require native AFWA throughout.
   [AFWA reduction](https://github.com/wrf-model/WRF/blob/v4.8.0/phys/module_diag_functions.F#L911-L962)
8. **P3 · Treat broader physics and geometry as separate changes.**
   The current pins and single Lambert domain define the supported
   surface. The pinned defaults are `icloud=1` and positive-definite
   advection. WRF forces four Noah-MP soil layers, and the Registry
   defaults to 21 land categories. Make a choice explicit when a new
   scheme, source, projection, nest, or solver revision needs it.
   The existing checks for theta/THM, humidity, rainfall windows,
   cloud overlap, rotation, hybrid coordinate, and source soil/height
   inventories hold. The DWD regular-grid comparison checked surface
   fields. Qualify upper-air remapping separately.
