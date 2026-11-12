# Adversarial review remediation plan

Validated against the producer checkout, the adjacent consumer checkout, the pinned
WRF and WPS sources, and the archived 3 km pilot. See the [project plan](plan.md)
for the release sequence.

## P0 · Native solver

- **History timing.** Keep `history_begin_h`. WRF 4.8 declares it in
  [`registry.io_boilerplate`](https://github.com/wrf-model/WRF/blob/v4.8.0/Registry/registry.io_boilerplate).
  The prior 22-hour pilot used a six-hour delay and produced the origin and
  16 leads. A key list copied from `Registry.EM_COMMON` would reject valid keys.
- **Image gate (also P1-8).** Run generated namelists through both native
  binaries offline in CI and require each to reach its first input open.
  Keep a real integration replay as the release check. CI can add a short
  end-to-end solve when it has a pinned, compact initialization fixture.
  The build applies zero standalone patches and includes a scripted Makefile
  dependency fix. The [build documentation](../docker/README.md) records this.

## P1 · Science, source input, and publication

1. **CAPE method.** The consumer-owned catalog and emitted Zarr metadata use
   the native AFWA most-unstable parcel definition. The producer contract test
   checks their agreement. Bind this method to the WRF layer when qualifying
   the consumer's Zarr adapter.
   [Pinned AFWA parcel search](https://github.com/wrf-model/WRF/blob/06d4240ae989cc3e50af412bb472df3d9048783c/phys/module_diag_functions.F#L556-L603)
2. **Vertical velocity.** Document `omega_pas` as an encoding of native geometric
   velocity using a hydrostatic density relation. A matching moist inverse
   recovers that encoded velocity. Physical pressure tendency in nonhydrostatic
   flow is a separate quantity. The consumer's dry inverse has no production
   call site in the adjacent checkout. Qualify a moist inverse or a versioned
   geometric-velocity field with its first caller.
3. **Gust.** Retain the IFS-derived turbulence increment with diagnosed WRF
   boundary-layer depth. Cite the derivation and test neutral, stable, and
   buoyant cases against the Monin–Obukhov form. Station skill remains an
   observation comparison.
4. **MSLP.** Name the continuous plateau variant accurately, keep good native
   AFWA cells, and use signed elevation for below-sea-level reduction. Pin
   numerical cases for cold, temperate, and hot profiles.
5. **Forcing (also P2-6).** Resolve GFS through a recipe and provide mirror-relative
   full-file keys to acquisition. Accept both active WPS soil temperature codes
   and reject collisions. The shipped Vtable maps MSLET; its alternate PRMSL
   entry is commented out. Use the WPS soil-level attribute and verify the
   complete dimension signature across the forcing window. Qualify ICON-EU or
   IFS with a real staged cycle, an exact WPS field and soil mapping, regional
   coverage, and source timing before enabling either. The model-level
   ICON path requires packing field files by timestep and carrying analysis-only
   fields forward as prescribed by its Vtable. [WPS variable tables](https://github.com/wrf-model/WPS/tree/v4.7.0/ungrib/Variable_Tables)
6. **Remote publication.** File storage is the implemented backend. Qualify
   private R2 with the archived GFS pilot. Agree on immutable attempt payload
   paths with the consumer, commit `_ready.json` conditionally, and update
   `latest.json` by compare-and-swap. A condition on the lease object protects
   that object alone. Test competing attempts, interrupted transfer, retry,
   object hashes, and tile range reads. Shared filesystem deployments require
   coherent exclusive locks. [R2 S3 API](https://developers.cloudflare.com/r2/api/s3/api/)
7. **History bytes.** The runtime I/O trial retained all fields consumed by post.
   It reduced the representative frame from 19.76 to 14.72 MB and increased the
   30-minute replay from 55.50 to 62.52 seconds. Keep the default stream. Repeat
   with a dedicated output stream if storage becomes the binding cost.
8. **Solver provenance and CI.** Covered under the native solver gate above.
9. **Solver tuning (also P2-7).** Keep the current science until paired skill
   and timing measurements support a change. Both configured domains reject
   46 and 48 eta faces at the existing stretch and 5,000 Pa model top.
   `AV_WRF_MPI_RANKS` already sets the deployment rank count recorded by the lock.
   In single 30-minute replays on the same arm64 image and archived inputs,
   radiation every 10 minutes took 55.61 s versus 55.50 s at three minutes
   and changed the frame. Four, eight, and 16 ranks took 55.50, 60.66, and
   65.75 s. Repeat across forecast cases and target machines before tuning.
10. **Publication validation.** Keep the independent decode, SHA and pressure
    checks at admission. A full-size fixture retiled and repeated archived pilot
    data over 40 leads through f048 for 878 objects and 825 MB of encoded data.
    On the arm64 solver image's Docker Desktop filesystem, three fresh publishes
    took 3.13 s median, with 3.12 s under lock and 1.01 s for decode and finite
    checks. A failure after 439 writes left `_ready.json` absent. Retry took
    2.44 s and all object hashes matched. On APFS, verified copy-on-write took
    2.92 s versus 2.61 s for independent writes. Independent files isolate
    published bytes from stage repairs. Use `--prune-stage` after the final
    destination to reclaim the staged bytes.

## P2 · Durability and contract boundaries

1. **Repeated scans.** Remove the repeated computational-volume finite sweep
   after native input admission. Preserve checks that validate published values
   and pressure columns.
2. **Local durability.** Persist object bytes and directory entries before the
   publication commit. On local APFS the median pilot publish rose from
   0.402 s to 0.483 s over three trials per version.
3. **Producer identity.** Bind new attempts to the installed producer source
   separately from the solver image. The hash covers every installed package
   `.py` file, including CLI and verification code. Run `plan` with a new
   attempt after changing that package. Post checks the running package against
   the lock. Publish requires the staged identity to match the lock, including
   for replay by a later publisher. Legacy attempts can replay when both
   records predate the digest. Add the published identity with the consumer
   receipt migration.
4. **Output layout.** Enforce the vendored Zarr layout for production profiles.
   Keep generic shard codec coverage for multiframe and padding behavior.
5. **Dateline.** Lambert x/y are projected coordinates and the consumer requires
   `wrapLongitude: false` for this grid. Forcing bounds use unwrapped longitude.
   Qualify source cutout and the consumer together on the first crossing domain.
6. **GFS variants.** Covered by the recipe and inventory changes above.
7. **MPI ranks.** Covered by solver tuning above.

## P3 · Cleanup

Reuse the producer gravity constant for synthetic geopotential. Enforce nonnegative
CAPE and PBL height at post. The current schema requires both pressure arrays,
so paired admission also checks positive surface pressure. Keep units and
constant choices with their numerical implementations until a shared wire
conversion requires coordinated ownership.

## ICON-EU forcing exercise

Use ICON-EU as initial and lateral boundary forcing for WRF. Configure a
separate product and model namespace for a side-by-side GFS run. A new recipe
registers its acquisition inventory, coverage and time checks, normalized GRIB
validator, Vtable, and metgrid requirements. The existing CLI accepts the
assembled inputs. WRF output follows the existing post and publication path.

1. **Choose a feed and window.** Qualify the deterministic 18Z native v1 feed.
   Check every required field through the last boundary hour and measure the lag
   to the complete inventory. A sampled v1 cycle offers hourly model-level
   pressure through f078, followed by three-hour steps. The pilot needs f022
   and the full run f054. Check the source footprint plus the WPS interpolation
   halo. DWD announced retirement of the legacy regular-grid tree after
   November 30, 2026. [DWD transition notice](https://www.dwd.de/DE/leistungen/opendata/neuigkeiten/opendata_september2026_1.html)
   [Sampled v1 pressure inventory](https://opendata.dwd.de/weather/nwp/v1/m/icon-eu/p/P/lvt1/150/lv1/74/r/2026-11-11T18%3A00/s/)
2. **Assemble WPS inputs.** Download the recipe's grouped field and level keys.
   Cache invariant objects and the remapping weights. Remap the triangular grid
   to a regular lat/lon cutout, verify the wind reference frame and masks, and
   repack CCSDS to a GRIB2 encoding accepted by WPS 4.7. Average the 75 `HHL`
   interfaces into 74 heights aligned with full-level pressure, temperature,
   humidity and wind. Write each assembled hour with its target valid time and
   retain the donor inventory for replay. [DWD data reference](https://dwd.de/DWD/forschung/nwv/fepub/icon_database_main.pdf)
   [WPS GRIB2 reader](https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/src/rd_grib2.F)
   [WPS packing reader](https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/src/ngl/g2/gf_unpack7.F)
3. **Qualify WPS and `real.exe`.** Use a recipe-owned `Vtable.ICONm` derivative
   with the 0.5 cm soil selector verified against the decoded layer metadata.
   Preserve `SPECHUMD` if enabling `use_sh_qv`. Check `SOILT` and `SOILM`
   dimensions, depths, and units. Preserve the GRIB centre so stock ungrib
   performs its DWD soil-moisture conversion. Confirm fractional land-mask
   treatment and an ice-fraction
   source or an explicit domain policy. Verify height and pressure alignment,
   support for the model top, and correct times through `met_em` and WRF input.
   The pressure-level route uses `Vtable.ICONp` and needs its own lower-column
   coverage check. [WPS ICON Vtable](https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/Variable_Tables/Vtable.ICONm)
   [WPS metgrid table](https://github.com/wrf-model/WPS/blob/v4.7.0/metgrid/METGRID.TBL.ARW)
   [WRF initialization](https://github.com/wrf-model/WRF/blob/v4.8.0/dyn_em/module_initialize_real.F)
4. **Qualify the product.** Run a short real-data slice through WPS and
   `real.exe`, then the paired pilot through WRF, post, and publish. Check all
   acquired and assembled hashes, the full boundary window, solver stability,
   and time to complete the cycle before enabling the profile. Keep remapping
   tools with acquisition and pin their versions and weights there.

## Verification

Both native executables reached input opening in the clean-snapshot gate.
The archived GFS replay validated 23 GRIB2 hours and their `met_em` files.
`real.exe` initialized with 34 atmospheric levels and four soil layers, and
the new `wrfinput` passed grid and vertical checks. Replaying post and publish
from sealed pilot output matched all 16 leads and 374 object hashes at each
of two local destinations. The second recipe fixture exercised grouped keys,
a custom Vtable, three-hour cadence, and more moisture layers than
temperature layers. The tree passed `uv run verify`.
