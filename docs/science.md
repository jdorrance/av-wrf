# Science, Mathematical Formulations, and Assumptions

`av-wrf` owns numerical weather integration (`WPS` + `WRF-ARW`) and deterministic
post-processing into [`wrf-zarr-v3`](../vendor/producer-contract/wrf-zarr-v3.schema.json).
Downstream `windy-av-layer` (`@av/thermodynamics`, `@av/soaring`, `@av/forecast`)
owns dry-parcel ascent (`dry-lenschow-1p2-model-levels-v2`), Deardorff $w_*$,
Lenschow & Stephens (1980) updraft profiles, Copernicus GLO-90 subgrid terrain
columns, sample packs, and tiles. The producer/consumer conventions follow.

## 1. Physical Constants: WRF Inversion vs. Consumer Parcel Evaluation

`av-wrf` and `windy-av-layer` deliberately use two distinct constant sets on
their respective sides of the Zarr v3 contract:

| Quantity | `av-wrf` (`core/thermo.py`, WRF internal) | `windy-av-layer` (`@av/thermodynamics`) | Reason for Split |
| :--- | :--- | :--- | :--- |
| $T_0$ ($\text{K}$, pert. offset) | `300.0` (`t0` in [`module_model_constants.F`](https://github.com/wrf-model/WRF/blob/v4.8.0/share/module_model_constants.F)) | — | WRF `T = \theta - 300` is fixed at `300 K` regardless of namelist `base_temp` |
| $R_d$ ($\text{J kg}^{-1}\text{ K}^{-1}$) | `287.0` ([`module_model_constants.F`](https://github.com/wrf-model/WRF/blob/v4.8.0/share/module_model_constants.F)) | `287.05` (WMO) | Exact inversion of WRF `T` / `THM` without upper-air drift |
| $c_{pd}$ ($\text{J kg}^{-1}\text{ K}^{-1}$) | `1004.5` ($= \frac{7}{2} R_d$, $\kappa = \frac{2}{7}$ exact) | `1004.0` ($\kappa = 0.2859$, mixed-layer `0.2854`) | Matches WRF RK3 equation of state to machine precision |
| $R_v$ ($\text{J kg}^{-1}\text{ K}^{-1}$) | `461.6` ($R_v / R_d = 1.608362$, $\delta = 0.608362$) | $\epsilon = 0.622 \implies \delta = 0.607717$ | Matches WRF `THM = \theta(1 + (R_v/R_d)q_v) - 300` |
| $L_v$ ($\text{J kg}^{-1}$) | `2,500,000` (`xlv` in WRF) | `2,501,000` (Bolton 1980) | Used in `av-wrf` only for convective gust buoyancy |
| Gravity $g$ ($\text{m s}^{-2}$) | `9.81` (`g` in WRF) | `9.80665` (WMO $g_0$) | Preserves `(PH + PHB) / 9.81 == HGT` at ground ($k_w = 0$) |
| Earth radius $R_\oplus$ ($\text{m}$) | `6,370,000` (`reradius` in WPS/WRF) | `6,370,000` (passed via `LambertConformalGrid`) | Single-owner Lambert descriptor eliminates projection drift |

- **Why `(PH + PHB) / 9.81` must not use `9.80665`:** WRF initializes base-state
  geopotential at the surface face as $\text{PHB}(k_w = 0) = 9.81 \cdot \text{HGT}$.
  Dividing by `9.80665` over a `4,100 m` massif would introduce a `+1.40 m`
  ground-datum offset relative to `terrain_m` (`HGT`), distorting near-surface
  `heightM - terrainM` AGL differences in `windy-av-layer`.
- **Vertical geoid datum (`EGM96` vs. `EGM2008`):** Stock WPS `topo_gmted2010_30s`
  (`dem_source: default`, [USGS OFR 2011-1073](https://pubs.usgs.gov/of/2011/1073/pdf/of2011-1073.pdf))
  uses `EGM96` with regional exceptions, whereas `Copernicus DEM DGED 2023_1 GLO-30`
  and `windy-av-layer` (`Copernicus GLO-90`) use `EGM2008`. `ReadyReceipt.grid`
  records `demSource` and `landuseSource` explicitly; internal WRF AGL heights
  `(PH + PHB)/9.81 - HGT` remain datum-consistent within WRF, and switching
  `dem_source` to Copernicus `GLO-30` in Phase 5 unifies both sides on `EGM2008`.

## 2. Temperature, Moisture, and Consumer Saturation Guards

1. **Sensible air temperature (`T` vs. `THM`):** Even with moist potential
   temperature dynamics (`use_theta_m = 1`, [Skamarock et al. 2019](https://www2.mmm.ucar.edu/wrf/users/docs/technote/v4_technote.pdf)
   Eq. 2.11), `wrfout` `T` stores dry perturbation potential temperature
   $\theta - 300\text{ K}$ (`WRF_T0_K = 300.0`, distinct from namelist `base_temp`),
   while `THM` stores $\theta_m - 300\text{ K} = \theta(1 + (R_v/R_d)q_v) - 300\text{ K}$
   ([WRF Users' Guide Ch. 5](https://www2.mmm.ucar.edu/wrf/users/docs/user_guide_v4/v4.4/users_guide_chap5.html),
   [WRF Forum #5569](https://forum.mmm.ucar.edu/threads/accessing-variables-on-head_grid.5569/)).
   The NetCDF reader checks `T` and `THM` on exported levels and rejects masked
   or invalid values at the file boundary.
2. **Specific humidity & omission of `rhPercent`:** WRF `QVAPOR` and `Q2` are
   water-vapor mixing ratios $r$ (`kg/kg dry air`), converted via $q = r / (1 + r)$.
   Model-level `rhPercent` is intentionally omitted because `windy-av-layer`
   (`packages/forecast/src/native-fields.ts`) computes dewpoint directly from
   `spfhKgKg` and `pressurePa`, matching `ICON-EU` and `RRFS`.
3. **2 m Bolton liquid-saturation guard (`clamp_spfh_2m_vec`):** The selected
   MYNN surface layer (`sf_sfclay_physics = 5`) supplies native `Q2`.
   `av-wrf` caps its converted `spfh_2m_kgkg` at consumer Bolton (1980) liquid saturation
   ($e_s = 6.112 \exp(17.67 T_c / (T_c + 243.5))\text{ hPa}$, $\epsilon = 0.622$)
   with $2^{-14}$ mantissa headroom so `keepbits = 15` rounding never exceeds `100%` RH.

## 3. Vertical Coordinate, Mountain Compression, & Consumer `20 m` / `1,000 m` Gates

1. **WRF `SUBROUTINE levels` (`auto_levels_opt = 2`) & cubic hybrid coordinate (`core/vertical.py`):**
   Following [Skamarock et al. (2019, Eqs. 2.2–2.6 & 5.3–5.7)](https://www2.mmm.ucar.edu/wrf/users/docs/technote/v4_technote.pdf)
   and [`dyn_em/module_initialize_real.F`](https://github.com/wrf-model/WRF/blob/v4.8.0/dyn_em/module_initialize_real.F),
   `auto_levels_opt = 2` (`e_vert = 55`) stretches domain-wide $\eta \in [1, 0]$
   levels using isothermal `tt = 290 K`, clamped `(dzmax/2 - dz)/(dzmax/2)` blend
   from `dzstretch_s` to `dzstretch_u`, and constant-`dz` upper fill down to
   `p_top_pa = 5,000 Pa`. WRF limits that fill to `1.5 * max_dz`. `hybrid_opt = 2`
   (`etac = 0.2`) maps the faces over terrain using
   $$p_d(\eta) = B(\eta)(\bar{p}_s - p_t) + [\eta - B(\eta)] \, (p_0 - p_t) + p_t$$
   `av-wrf check` and `plan` verify `e_vert` reaches `p_top_pa` and $\Delta p_d > 0$
   across `terrain_elevations_m`. The reference column uses WRF's default 200 K
   isothermal floor. `solve` verifies `ZNW` and checks initialized column geometry
   immediately after `real.exe`. `post` and `publish` enforce column pressure order.
2. **Lowest mass midpoint vs. consumer `minimumLevelAglM = 20 m`:** `windy-av-layer`
   (`NATIVE_DIAGNOSTIC_PARAMETERS.minimumLevelAglM = 20`) discards any model level
   at or below `20.0 m` AGL. Shipped domain profiles use `dzbot_m = 50.0` (placing
   level 0 at `~25.3 m` AGL in valleys and `~21.9 m` AGL even over `5,137 m` Mount
   Ararat), and both `vertical.py` and `post.py` reject `level[0] <= 20.0 m` AGL
   and count only usable levels in `(20, 2000]` m AGL toward `min_bl_levels_2km_agl`.
   On the archived pilot's 38 exported levels, reducing `dzstretch_s` from 1.11
   to 1.09 raised the minimum count from 17 to 19 and lowered the minimum column
   top from 6.43 km to 4.98 km AGL.
3. **Summer thermal-expansion headroom (`max_dz_m <= 900 m` vs. `1,000 m`):**
   `windy-av-layer` (`packages/soaring/src/column.ts:159`) rejects an **entire
   column as `unavailable`** if any adjacent level gap exceeds `maximumProfileGapM = 1,000 m`.
   `vertical.py` enforces `max_dz_m <= 900 m` (`850 m` in shipped domain profiles),
   and `post.py` verifies actual exported `height_msl_m - terrain_m` gaps.

## 4. Winds, Vertical Velocity, MSLP, Surface Fluxes, and CAPE

1. **Grid-to-Earth wind rotation & solver vs. diagnostic CFL (`core/namelist.py`, `runner/solver_exec.py`):**
   WPS computes `SINALPHA` with an unsigned Lambert cone. Geography generation
   restores the southern sine sign on mass and staggered grids before hashing.
   Solve checks those signs before running WPS, and post normalizes imported WRF
   wind coefficients. [WPS `get_rotang`](https://github.com/wrf-model/WPS/blob/v4.7.0/geogrid/src/process_tile_module.F#L1873-L1895)
   Actual RK3 substep stability is gated on `namelist.py` (`\Delta t \le 6 \Delta x_{\text{phys}}`),
   solver logs, and `wrf.exe` completion. `DiagnosticsReceipt.max_cfl`
   uses grid-relative output winds and nominal cell spacing as a diagnostic proxy.
2. **Hydrostatic pressure-velocity proxy (`geometric_w_to_omega_vec`):** The producer
   encodes native WRF geometric $w$ as $-\rho_m g w$ using moist density and WRF
   constants. A matching inverse recovers $w$ within storage rounding. Material
   pressure velocity also depends on pressure evolution and advection.
   The consumer's dry-density inverse underestimates $|w|$ by about 0.55% at
   `q = 0.01 kg/kg`.
3. **10 m gust and flux time support:** The endpoint gust estimate adapts the
   IFS turbulence term with native `PBLH` and a virtual-temperature flux from
   `HFX` and `LH`. It represents boundary-layer turbulence at the valid time.
   [IFS Cy43r1 §3.10.4, equations 3.97–3.99](https://doi.org/10.21957/sqvo5yxja)
   Exported sensible and latent fluxes use signed `ACHFX` and `ACLHF` differences.
   Precipitation uses bucket-adjusted `RAINC + RAINNC`. All three accumulators
   share the full interval between published valid times, so tail rain totals
   can be recovered by integrating the reported rate. A shorter heat window
   requires its own time support in the consumer contract.
4. **Plateau MSLP:** Positive native `AFWA_MSLP` values take precedence. Missing
   or nonpositive cells use the same sea-level pressure reduction and Shuell
   correction, reconstructed from the lowest mass level and surface pressure.
   [Native WRF AFWA MSLP](https://github.com/wrf-model/WRF/blob/06d4240ae989cc3e50af412bb472df3d9048783c/phys/module_diag_functions.F#L911-L962)
5. **CAPE provenance (`afwa_buoy_opt = 1`, strict `AFWA_CAPE_MU`):** Enabling
   `afwa_buoy_opt = 1` in `&afwa` ([`registry.afwa`](https://github.com/wrf-model/WRF/blob/v4.8.0/Registry/registry.afwa))
   computes `AFWA_CAPE_MU`. WRF initializes the parcel at the lowest model level
   and searches for maximum equivalent potential temperature at pressures above
   the absolute `500 hPa` cutoff. [WRF `Buoyancy`](https://github.com/wrf-model/WRF/blob/06d4240ae989cc3e50af412bb472df3d9048783c/phys/module_diag_functions.F#L556-L603)
   The Zarr group and producer contract record this method.

## 5. Comparison Against Dr. Jack Glendening (RASP / BLIPMAP) & Operational Caveats

- **Surface parcel excess (`T2` vs. RASP fixed $\Delta T$):** The selected MYNN
  surface layer (`sf_sfclay_physics = 5`) diagnoses `T2`.
  `windy-av-layer` owns the parcel choice `parcelSurfaceDeltaTC = 0 K`;
  its suitability requires observation scoring.
- **Topographic radiation (`slope_rad = 1`, `topo_shading = 1`) vs. RASP `sfcsun`:**
  3 km slope aspect and `25 km` cast shadows enter Noah-MP's surface energy budget
  directly (`radt = 3 min`) without consumer double-counting.
  The [Noah-MP patch](../docker/patches/README.md) retains valid WRF albedo
  across steps with no incident shortwave so RRTMG receives a reflectance at dawn.
  Live post requires `SWDNB`, `SWUPB`, and `ALBEDO` at saved RRTMG times and
  checks same-plane flux bounds and illuminated albedo. Sparse archive reads may omit them.
- **Cold-start land state, snow, & high-altitude inland lakes:** Phase 1
  initializes Noah-MP soil layers from the selected forcing and integrates
  through the configured spin-up. Warm land-state cycling needs observation
  qualification.

## 6. Second-Pass Physics & Wire Decisions (2026-11)

- **MYNN-EDMF default:** WRF v4.8 couples subgrid cloud to radiation with
  `icloud = 1` and `icloud_bl = 1`. `CLDFRA_BL` contains the current total cloud
  fraction; coupled radiation copies it into `CLDFRA` at radiation calls.
  Live post requires and uses `CLDFRA_BL` with MYNN and `icloud_bl > 0`, so
  dissipated cloud clears between calls. Other schemes, disabled coupling and
  sparse archives without `CLDFRA_BL` use `CLDFRA`.
  [WRF radiation driver](https://github.com/wrf-model/WRF/blob/v4.8.0/phys/module_radiation_driver.F#L1403-L1416)
- **Lake physics:** The CLM lake model uses `LAKE_DEPTH` through `geo_em` and
  `met_em`. `sf_lake_physics`, `use_lakedepth`, `lakedepth_default`, and
  `lake_min_elev` carry one value per active domain. Explicit lake categories
  bypass WRF's elevation cutoff. Configured `ocean_seeds_lat_lon_deg` identify
  four-connected water bodies that use external water temperatures: geography
  sealing transfers their lake fractions and category to `ISWATER`, preserving
  terrain, water coverage and other lakes. Solve rejects uncorrected geography.
  [NCAR's MODIS sea classification guidance](https://forum.mmm.ucar.edu/threads/how-to-change-land-use-category-for-black-sea.10277/)
  The configured geography resolution selects the LAI and VAR_SSO tables.
- **Steep-eta 6th-order filtering (`diff_6th_slopeopt = 1`,
  `diff_6th_thresh = 0.10`):** Suppresses cross-inversion mixing along tilted
  eta surfaces over Ararat/Aragats while retaining the 6th-order filter on
  flat terrain.
- **Adaptive RK3 stepping (`use_adaptive_time_step = .true.`,
  `target_cfl = 1.2`, `target_hcfl = 0.84`):** Registry `max_domains` arrays
  (`target_cfl`, `target_hcfl`, `min/max_time_step`, `starting_time_step`) carry
  one value per domain; `use_adaptive_time_step`, `step_to_output_time`, and
  `adaptation_domain` stay scalar. `ICLOUD` and `ICLOUD_BL` are also scalar.
  Shin-Hong mixing and gravity-wave drag options carry one value per domain.
- **Concentric 1-way nest:** GFS `0.25°` and IFS `0.1°` exceed `3.5 x` the
  `3 km` child spacing, so `parent_grid_ratio = 3` builds a `9 km` d01 buffer
  with symmetric `parent_halo_cells`; ICON-EU `0.0625°` runs single-domain on
  the same published child coordinates. d01 retains a positive history clock
  for adaptive alignment and delays history writes past the run. WPS generates
  only the initial child input.
- **Output diagnostics:** `initial_dps_dt_hpa_hr` measures the first six
  published intervals after spin-up. The sampled Courant proxy uses saved
  `DT`, staggered face thickness and center Lambert scale. WRF solver logs
  report integration stability.
