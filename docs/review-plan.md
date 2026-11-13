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

Reference systems inspected: `soaringmeteo/soarWRF` (18/6/2 km 3:1),
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
