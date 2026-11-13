# Review evidence

The [project plan](plan.md) owns release gates. This page preserves the November
2026 pilot evidence; [remediation](review-remediation.md) records current status
and remaining qualification. Archived timings predate current nesting and
profile changes unless stated.

## Native pilot

The local ARM64 pair used the 2026-11-11 18Z GFS and ICON-EU cycles, the same
geography and physics, four MPI ranks, and byte-identical native executables.
That archived pair used YSU and surface-layer scheme 1. The current MYNN/lake/
adaptive suite has separate qualification.
Each integrated 22 hours and published 16 leads on the 84 × 84 interior.
Post and publish replays preserved all 748 Zarr object hashes.

The private input handoff cold-started the same ICON-EU cycle in a rebuilt
ARM64 image. The eight-hour run published two leads and verified all 80
object hashes. All 29 arrays matched the corresponding full-pilot data.
Across those leads, mean absolute differences from the paired GFS run were
1.00 K at 2 m and 0.56–0.63 m/s for 10 m wind components.

The image gate runs both native executables through namelist parsing for every
product and checks resolved per-domain options. Synthetic single-domain and
nested inputs exercise WPS, WRF and postprocessing. Archived GFS inputs yielded 34
atmospheric levels and four soil layers. ICON-EU yielded 75 atmospheric levels
and eight soil temperature and moisture layers. WRF 4.8 accepts
`history_begin_h` from [its I/O registry](https://github.com/wrf-model/WRF/blob/v4.8.0/Registry/registry.io_boilerplate).

## Albedo replay

The patched ARM64 image replayed both 16-lead pilots. WRF initial states,
boundary files, and integration namelists matched the archived runs byte for
byte. Before sunrise, native 2 m temperatures also matched exactly. The
[Noah-MP guard](../docker/patches/README.md) eliminated negative shortwave
in every saved frame, including the full computational grid. The previous
published interiors contained 8,990 GFS and 8,120 ICON-EU negative slope
shortwave samples. Both corrected counts were zero.

A three-minute GFS replay showed valid albedo before first light at 03:45Z.
The first reflected-to-incident shortwave ratio matched the preceding
albedo within 3.6 × 10⁻⁷. All 21 samples from 03Z to 04Z passed.

The ICON-EU minus GFS mean absolute 2 m difference fell from 1.56 to
0.78 K and the maximum from 18.32 to 6.38 K. At the original outlier
cell, the gap changed from +18.32 to −2.15 K. Corrected 10 m wind speed
mean absolute difference was 0.74 m/s. At one +16 h cell, native wind
speeds were 0.47 and 10.93 m/s, with parameterized gusts of 0.53 and
20.17 m/s. At +13 h, one cell had a 905 m boundary-layer depth difference.
The paired replay measures disagreement; observation scoring is separate.

Reproduce the paired field and radiation audit from local artifacts.
The baseline and candidate must share grid and time coordinates.
Field differences use corresponding native model levels.

```sh
uv run python -m scripts.audit_forecast "$ready" --baseline "$baseline" \
  --wrfout-dir "$wrfout_dir" --solver-receipt "$solver_receipt" \
  --require-clean-radiation
```

The hardened image retained the same WRF, real, and metgrid binaries.
The smaller ungrib build reproduced all 46 archived WPS intermediates
byte for byte. Its nonroot two-lead cold start verified 80 published
objects, solve reuse, and publish retry after pruning.

## ICON-EU forcing

DWD's v1 feed uses the native triangular grid. DWD scheduled the legacy
regular-grid feed to end after November 30, 2026.
[DWD transition notice](https://www.dwd.de/DE/leistungen/opendata/neuigkeiten/opendata_september2026_1.html)

The [acquirer](../src/av_wrf/runner/README.md) reuses grid weights and static
fields across forecast hours. It remaps to a regular cutout, checks that
pressure brackets the WRF model top, and averages the 75 height interfaces
onto 74 full levels. The archived paired pilot used 9,187 donor objects and
produced 236 MB of normalized GRIB2 across 23 boundary files. Direct WPS v5
intermediates now provide an implemented route with the same remapping and
admission checks; the archived byte count describes the GRIB route.
[DWD ICON reference](https://dwd.de/DWD/forschung/nwv/fepub/icon_database_main.pdf)

A same-cycle check matched 7,176 cutout points to DWD's regular grid.
Across f000 and f008, mean absolute differences were 0.20–0.21 K for 2 m
temperature and 0.17–0.25 m/s for each 10 m wind component. Land classification
agreed at 7,151 points. At f000, surface pressure and terrain differences had a
correlation of −0.997. A hypsometric reduction to the reference terrain lowered
the mean absolute pressure difference from 4.81 to 0.23 hPa.
[DWD regular-grid data](https://opendata.dwd.de/weather/nwp/icon-eu/grib/18/)

Across four cycles, the latest sampled f022 pressure, temperature, or soil
object was modified 2 h 49 m after initialization. The 3.25 h arrival
allowance remains provisional until complete inventories are observed over
more cycles.
[DWD v1 listing](https://opendata.dwd.de/weather/nwp/v1/m/icon-eu/p/P/lvt1/150/lv1/74/r/2026-11-11T18%3A00/s/)

The archived GRIB route used stock WPS `FLAG_SOIL_LEVELS` and DWD's center code
for volumetric soil moisture conversion. The pilot donor cutout had zero ice
thickness. Ice-bearing regions require qualified coverage fractions.
[WPS conversion](https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/src/rrpr.F#L793-L802)

## Measured tradeoffs

- **History stream.** Trimming the consumed fields reduced a representative
  frame from 19.76 to 14.72 MB and increased a 30-minute replay from 55.50 to
  62.52 s. Broad field pruning was rejected for that replay. Current MYNN runs
  add `CLDFRA_BL` through `iofields_filename` to the standard history stream.
- **Solver.** Four, eight, and 16 ranks took 55.50, 60.66, and 65.75 s in
  single 30-minute replays on one ARM64 image. Ten-minute radiation took
  55.61 s and changed the frame. The current configuration uses the
  three-minute setting.
- **Archived production geometry.** A cold GFS hour on the 196 × 164 computational
  grid took 215.6 s to solve with four ranks and a 15 s time step.
  The WRF process took 208.8 s, including 149.1 s in integration steps.
  Post and local publication took 0.92 s and 0.23 s. This nighttime
  scratch run used a zero-spin-up override and predates the current parent
  buffer and geometry. All 55 GFS forcing files passed
  native initialization for the full 54-hour bracket in 206.1 s.
- **Publish.** A retiled 40-lead fixture held 878 objects and 825 MB. ARM64
  Docker Desktop median publish took 3.13 s, including 1.01 s for decode and
  finite checks. A failure after 439 writes left the ready receipt absent.
  Retry took 2.44 s and all hashes matched. On APFS, verified copy-on-write
  took 2.92 s versus 2.61 s for independent writes.

## Expanded full-horizon run (2026-11-13)

The corrected nested GFS run completed 6 spin-up hours and a 72-hour forecast
through 2026-11-16 00Z. Its 204 × 174 published grid has 40 levels and 48 leads;
the area increased 31.5% from the preceding profile. All 73 hourly native frames
passed face-order checks. Independent Zarr decoding verified 22 finite fields
and all 1,046 object hashes. All 73 native radiation frames passed unchanged
gates across 1,240,314 daylight samples. The diagnostics digest matched its ready receipt.

On an Apple M4 Max with Docker configured for 12 CPUs and 75 GiB, solve took
4 h 46 min 28 s, used 56.77 CPU-hours and peaked at 14.48 GiB of cgroup memory.
The paced GFS acquisition took 36 min 49 s for 282.77 MB; cached revalidation
took 22 s. Full native face checks took 8 s, post 31 s and publication 7 s.
The forecast holds 1.29 GB; retained solver work holds 11.44 GB of logical data.
Local compute incurred no purchased-instance charge; electricity was unmeasured.

Publication completed at 04:30:38Z. The cycle was archived, and the 04:30Z
deadline correctly withheld latest promotion. This cycle establishes cost and
physical admission; operational delivery still needs runtime margin and repeated cycles.
Independent audits ran against the frozen source on the host, where the development
Zarr reader is installed. The archived timestep-zero placeholders were unmeasured;
future diagnostics omit them while legacy receipts remain readable.

- Config SHA-256: `76f3312081fb8302bd6c01696fe36ea032d657af3d9551da4752af8d24b230ee`.
- Image ID: `sha256:52ac9ff4ecf33d15cdc761b449dd1016c6688199491de45bfa0c99b131dc6c7c`.

## Reference systems and implemented decisions (2026-11)

Reference systems inspected: SoaringMeteo's operator guide (18/6/2 km 3:1),
`sfalmo/rasp-from-scratch` (MYNN2.5, adaptive CFL, SRTM/CORINE),
`DavidRasp/blipmaps.nl` (GFS+ICON, sidecar post), `canadarasp.ca` (S3 `.idx`
byte ranges), `raspit`/`raspnw` (ephemeral VMs, legacy NCL). Lessons applied
here: 3:1 parent buffers, MYNN-EDMF with subgrid cloud fraction, 30s static
fields and adaptive RK3. Local publication retains per-object directory sync
and durable commit/recovery fences.

Thompson table prebaking, GFS/IFS indexed range fetching, direct ICON WPS
intermediates, and S3 conditional writes are implemented. Competitor designs
provide architectural references; the paired replays measure numerical behavior
and source disagreement. Delivery margin, live R2/consumer delivery and
observation scoring remain [qualification gates](review-remediation.md#remaining-qualification).

## External nesting evidence (researched 2026-11-13)

These sources inform the [nesting implementation and experiments](plan.md#5-configurable-nesting-implementation-plan).
Operator histories establish operational experience; papers establish results for
their tested cases. Repository configurations establish what was implemented.
None establishes forecast skill for our target region. Dates below refer to the
publication or reported experiment, rather than search-engine crawl dates.

### Regional soaring operators and source code

- **CanadaRASP: evaluate operational value.** Its operator reports that 400 m WRF
  forecasts in 2013–2014 revealed useful valley-flow detail but regularly
  overpredicted local winds. Slow tuning/evaluation and hardware failures led to
  retirement of WRF and use of HRDPS output. The changelog also records wind-arrow
  rotation fixes in June 2026 and source-format migration failures. These are
  self-reported outcomes, with no paired verification dataset on that page.
  Preserve direct-model baselines, fast experiments, rotation tests, and source
  migration fixtures. [History and changelog](https://www.canadarasp.com/aboutcanadarasp.html),
  [public processing code](https://github.com/ajberkley/canadarasp).

- **SoaringMeteo: nested coverage and maintainability.** Its 2013 operator guide
  describes 18/6/2 km grids with several fine regions and unreliable edge rows.
  The later soarV2 migration explains that maintenance and evolution of the old
  system had become difficult; reducing computation and extending coverage were
  explicit goals. Shared parents and declarative regions fit that experience.
  Its historical five-row edge warning describes those grids; our buffers require
  their own validation. [2013 guide](https://soarwrf4.soaringmeteo.org/soarWRFEN.pdf),
  [migration rationale](https://soaringmeteo.org/towards-soarV2/),
  [soarV2 processing source](https://github.com/soaringmeteo/soaringmeteo).

- **wxToFly lineage and newer RASP code: inspect the actual implementation.**
  Historical `raspnw-v2` contains wxToFly/RASP-NW files. The newer `rasp-windgram`
  credits TJ Olney's windgram design; its October 2026 snapshot documents shared
  Lambert placement and real-geogrid alignment tests. It also reports footprint
  changes causing mountain instability and NaNs despite a completion message.
  Adopt coordinate/finite-state checks and controlled case experiments. Its
  square-domain/fixed-buffer heuristics remain unqualified choices. This snapshot
  does not establish the current wxToFly deployment or a verified Git fork.
  [Historical code](https://github.com/nw-paragliding/raspnw-v2),
  [pinned design and failure investigation](https://github.com/nw-paragliding/rasp-windgram/blob/326891595937107871f643bcd8c0187aecb5a4bc/ARCHITECTURE.md).

- **RASP configuration diversity:** a June 2026 `rasp-from-scratch` configuration
  uses 10/2 km, ratio 5, two-way feedback, and different parent/child cumulus
  settings. It establishes an implemented alternative, without proving superior
  skill. Our proposed one-way/ratio-3 policy selects an initial subset of WRF; future
  ratios and physics combinations need their own tests.
  [Pinned namelist](https://github.com/sfalmo/rasp-from-scratch/blob/96d9dd4c8b4608603edf40593ab306672091cd8e/rasp/TIR/namelist.input).

- **XC Therm / Regtherm: represent the processes controlling thermals.** The
  operator describes terrain-defined regions, valley volume, moisture transport
  between regions, seasonal vegetation, and shading by all cloud layers. Regtherm
  derives soaring guidance from upstream weather models. This supports choosing
  physically connected focus areas and preserving radiation, moisture, and
  vertical profiles at the producer boundary. The public description supplies no
  independent comparative skill score. [Method description](https://xctherm.com/en/regtherm).

- **SkySight: distinguish native and derived resolution.** Its 9 September 2026
  announcement describes 100 m wind/ridge guidance produced by a CFD-trained AI
  downscaler driven by 900 m weather-model output; it lists thermal models at
  1–2 km. Reported accident-case backtests are operator claims without a released
  benchmark there. The architectural lesson is separate qualification of native
  forecasts and terrain-flow downscaling. The advertised 100 m product supplies
  no justification for a 100 m full-WRF integration here.
  [Technical announcement](https://skysight.io/news/100-metre-wind-and-ridge-forecast/).

- **MeteoSwiss: qualify changes through observations and delivery.** Its 2026
  ICON deployment paper reports scheduled comparisons beginning in 2021 before
  operational replacement in May 2024. Several scores improved while cloud-cover
  skill and valley-temperature/precipitation deficiencies remained. It separates
  computational consistency tests from multi-season observational verification
  and measures end-to-end runtime against a delivery deadline. Adopt that
  separation at a smaller scale; its GPU speedups do not predict WRF performance.
  [Deployment and verification study](https://gmd.copernicus.org/articles/19/755/2026/).

### Controlled scientific comparisons

- **Wagner, Gohm and Rotach (2014), idealized valley.** WRF comparisons included
  50 m LES and 1–10 km grids, with additional matched smoothed-terrain runs.
  Strong smoothing removed inversion/circulation structure; much of the change
  arose from lost valley geometry. The particular valley was 20 km wide and
  1.5 km deep. Inspect cross-sections, valley depth/volume, and passes as well as
  summits; its resolution result is conditional on that geometry.
  [Study](https://journals.ametsoc.org/abstract/journals/mwre/142/9/mwr-d-14-00002.1.xml).

- **Horvath et al. (2012), Nevada mountain winds.** Downscaling to 1 km and
  333 m improved vertical shear relative to coarser grids, but wind-speed bias
  and RMSE did not improve systematically; 333 m generally added little over
  1 km. Evaluation used four towers in two months. Fine-domain extent, roughly
  900 m source terrain, and inherited boundary errors were possible limitations
  identified by the authors; their individual effects were not isolated. Score
  processes and domain placement alongside aggregate surface-wind error.
  [Study](https://doi.org/10.1029/2012JD017432).

- **Bonekamp, Collier and Immerzeel (2018), Himalayan valley.** In two seasonal
  periods, 500 m improved temperature/wind agreement while precipitation errors
  persisted. Of 12-hour, 24-hour, three-day and five-day spin-ups, 24 hours worked
  best; longer spin-up did not consistently help. Land-cover changes also altered
  precipitation. Test our initialization duration and surface data separately;
  this result does not establish a universal 24-hour requirement.
  [Study](https://journals.ametsoc.org/view/journals/hydr/19/10/jhm-d-17-0212_1.xml).

- **Sabetghadam, Fletcher and Erler (2025), Canadian snow.** One-way 9/3/1 km
  WRF/Noah-MP simulations were evaluated over the 2017–2018 water year, including
  nine snow-pillow stations. Mean peak SWE underestimation was about 6% at 3 km
  and 3% at 1 km; annual SWE remained underestimated. Elevation mismatch was
  associated with snow bias. This is evidence for a useful refinement experiment,
  with different forcing, physics, and duration from our short-range forecasts.
  [Study](https://hess.copernicus.org/articles/29/887/2025/).

- **Angevine et al. (2020), boundary-layer gray zone.** In 600 m shallow-cumulus
  experiments, the tested reduction in parameterized MYNN mass flux performed
  worse: resolved motion failed to compensate and clouds developed late with too
  little liquid water. This concerns particular WRF 4.1 cases. Retain the current
  coupled scheme during the nesting comparison and test any physics change
  independently against cloud timing and mixing.
  [NOAA-hosted paper](https://repository.library.noaa.gov/view/noaa/29421).

- **Nardon et al. (2026), paragliding validation.** A roughly 104 m WRF study
  used 170 tracks over eight selected summer days. Thermal occurrence was strongly
  underforecast; more localized explicit structures gave mixed skill changes.
  Flight coverage was sparse and the study did not compare horizontal resolutions.
  It demonstrates useful flight-data verification and the limits of detailed
  deterministic thermal maps. Score sampled air motion separately from soaring
  diagnostics; retain observation masks and distinguish lift mechanisms.
  [Study, 13 October 2026](https://link.springer.com/article/10.1007/s00703-026-01215-x).

- **Abolafia-Rosenzweig et al. (2025), snow and temperature.** Revised snow
  parameterizations reduced western-US maximum-temperature cold bias by 17–24%,
  but a deliberately extreme snow-cover underestimate still left mountain cold
  bias. Snow error was insufficient to explain the whole problem. Audit snow,
  radiation, temperature, and turbulent fluxes together before selecting a fix.
  [Study](https://doi.org/10.1029/2025JD044191).

### WRF operational constraints and failure reports

- **Domain placement:** NCAR recommends ample parent coverage, room upwind, and
  boundaries away from complex terrain. A reported 9/3 km mountain case retained
  boundary instability after timestep reduction. Treat geometric alignment as
  necessary and terrain/flow review as a separate admission step.
  [WPS guidance](https://www2.mmm.ucar.edu/wrf/site/namelist.wps_best_practices.html),
  [operator case and support response](https://forum.mmm.ucar.edu/threads/nesting-boundary-over-mountainous-terrain.10245/).

- **MPI and offline nesting:** NCAR's processor guidance makes the smallest
  domain constrain useful parallelism; its 25-cell patch guideline is a starting
  heuristic. Benchmark the entire tree at 6 and 12 ranks. Offline `ndown` can
  reuse parents but handles one child per invocation and requires its own input
  and history workflow. Add it when measured reuse savings justify that workflow.
  [Processor guidance](https://forum.mmm.ucar.edu/threads/choosing-an-appropriate-number-of-processors.5082/),
  [multiple-child support response](https://forum.mmm.ucar.edu/threads/using-ndown-to-multiple-wrfinput.17530/).

- **Snow initialization:** a documented GFS/HRRR snow merge produced inconsistent
  SWE/depth and a Noah-MP water-budget failure. A 2026 report of rapid snow-cover
  rebound after MODIS initialization remains unresolved. These support tracing
  surface state through preprocessing and spin-up; they establish no generally
  valid snow correction. [Consistency failure](https://forum.mmm.ucar.edu/threads/noah-mp-error-using-hrrr-snow-cover.10016/),
  [unresolved rebound report](https://forum.mmm.ucar.edu/threads/unexpected-increase-in-snowc-after-modis-based-snow-initialization-in-wrf-noah-mp.28523/).

### Transfer limits

Public evidence does not support a controlled accuracy ranking across these
services. Operator descriptions, isolated incidents, idealized LES, annual
reanalysis downscaling, and short-range forecasts answer different questions.
Borrow explicit geometry, independent validation, reusable processing, and
failure handling. Qualify physical settings on our own held-out cases. Advertising
resolution, attractive maps, source-code availability, and native completion
alone are insufficient evidence of local forecast skill.
