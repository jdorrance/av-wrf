# Science, Mathematical Formulations, and Assumptions

`av-wrf` owns numerical weather integration (`WPS` + `WRF-ARW`) and deterministic
post-processing into [`wrf-zarr-v3`](../vendor/producer-contract/wrf-zarr-v3.schema.json).
Downstream `windy-av-layer` (`@av/thermodynamics`, `@av/soaring`, `@av/forecast`)
owns dry-parcel ascent (`dry-lenschow-1p2-model-levels-v2`), Deardorff $w_*$,
Lenschow & Stephens (1980) updraft profiles, Copernicus GLO-90 subgrid terrain
columns, sample packs, and tiles. Every physical constant, approximation, and
boundary assumption between the two repositories and published literature is
documented below.

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
   `codecs/wrf_nc.py` cross-checks `T` and `THM` across every cell on every slice.
2. **Specific humidity & omission of `rhPercent`:** WRF `QVAPOR` and `Q2` are
   water-vapor mixing ratios $r$ (`kg/kg dry air`), converted via $q = r / (1 + r)$.
   Model-level `rhPercent` is intentionally omitted because `windy-av-layer`
   (`packages/forecast/src/native-fields.ts`) computes dewpoint directly from
   `spfhKgKg` and `pressurePa`, matching `ICON-EU` and `RRFS`.
3. **2 m Bolton liquid-saturation guard (`clamp_spfh_2m_to_liquid_sat`):** WRF's
   Revised MM5 surface layer (`sf_sfclay_physics = 1`) diagnoses `Q2` from
   Monin–Obukhov similarity without liquid-water saturation adjustment, so `Q2`
   can exceed `105%` RH WRT liquid water in nocturnal radiation or freezing fog.
   `av-wrf` caps `spfh_2m_kgkg` at consumer Bolton (1980) liquid saturation
   ($e_s = 6.112 \exp(17.67 T_c / (T_c + 243.5))\text{ hPa}$, $\epsilon = 0.622$)
   with $2^{-14}$ mantissa headroom so `keepbits = 15` rounding never exceeds `100%` RH.

## 3. Vertical Coordinate, Mountain Compression, & Consumer `1,000 m` Gap Limit

1. **Log-pressure stretching & cubic hybrid coordinate (`core/vertical.py`):**
   Following [Skamarock et al. (2019, Eqs. 2.2–2.6 & 5.3–5.7)](https://www2.mmm.ucar.edu/wrf/users/docs/technote/v4_technote.pdf)
   and [WRF Real-Data Initialization](https://www2.mmm.ucar.edu/wrf/site/users_guide/real_initialization.html),
   `auto_levels_opt = 2` (`e_vert = 55`) stretches domain-wide $\eta \in [1, 0]$
   levels in log-pressure (`T0 = 300 K`) down to `p_top_pa = 5,000 Pa`, and
   `hybrid_opt = 2` (`etac = 0.2`) maps them over elevated terrain via:
   $$p_d(\eta) = B(\eta)(\bar{p}_s - p_t) + [\eta - B(\eta)] \, (p_0 - p_t) + p_t$$
   `av-wrf check` verifies `e_vert` reaches `p_top_pa` and $\Delta p_d > 0$ across
   `terrain_elevations_m`, while `wrf_nc.py` validates solver-emitted `ZNW` and
   `post.py`/`publish.py` enforce `pressure_pa[0] <= surface_pressure_pa` and
   strictly decreasing column pressure across every cell.
2. **Summer thermal-expansion headroom (`max_dz_m <= 900 m` vs. `1,000 m`):**
   `windy-av-layer` (`packages/soaring/src/column.ts:159`) rejects an **entire
   column as `unavailable`** if any adjacent level gap exceeds `maximumProfileGapM = 1,000 m`.
   `vertical.py` enforces `max_dz_m <= 900 m` (`850 m` in shipped domain profiles),
   and `post.py` verifies actual exported `height_msl_m - terrain_m` gaps.
3. **First mass midpoint vs. consumer `minimumLevelAglM = 20 m`:** With
   `dzbot_m = 34 m` ($\ge 25\text{ m}$ coupled surface-layer stability floor per
   [WRF Forum #8325](https://forum.mmm.ucar.edu/threads/increase-eta-level-and-decrease-dzbot.8325/)),
   the lowest mass midpoint sits at `~15–17 m` AGL.

## 4. Winds, Vertical Velocity, MSLP, Gusts, Clouds, and CAPE

1. **Grid-to-Earth wind rotation & output-frame Courant proxy (`core/namelist.py`, `core/post.py`):**
   WRF `SINALPHA = -sin(theta_lambert)` where $\theta = n(\lambda - \lambda_0)$
   ([`wrf-python` #142](https://github.com/NCAR/wrf-python/issues/142)). Actual RK3
   substep stability is gated on `namelist.py` (`\Delta t \le 6 \Delta x_{\text{phys}}`)
   and `wrf.exe` completion; `DiagnosticsReceipt.max_cfl` records an explicit
   output-frame Courant proxy over finite exported fields.
2. **Hydrostatic $\omega$ round-trip (`geometric_w_to_omega_pas`):** Nonhydrostatic
   WRF outputs geometric $w = dz/dt$ (`m/s`). `av-wrf` converts via hydrostatic
   $\omega = -\rho_m g w$ so `windy-av-layer` (`geometricWFromOmegaMps`) recovers
   mountain-wave $w$ cleanly.
3. **10 m wind gusts (`parameterized_gust_10m_mps`):** Aggregates `WSPD10MAX`
   across every hourly history dump in `(t_prev, t_curr]` (so `3 h` tail steps
   retain peak gusts from intermediate hours) and combines with the
   [Panofsky et al. (1977)](https://www.pmf.unizg.hr/_download/repository/tb1_ch2.pdf) /
   [ECMWF IFS `10fg`](https://codes.ecmwf.int/grib/param-db/49) turbulent-convective
   increment $\Delta V_{\text{turb}} = 7.71 (u_*^3 + \frac{0.5 \kappa}{12} w_*^3)^{1/3}$
   using actual surface density $\rho_{\text{sfc}} = p_{\text{sfc}} / (R_d T_{v0})$.
4. **Plateau MSLP (`AFWA_MSLP` / `hypsometric_mslp_vec`):** Uses WRF's native
   `AFWA_MSLP` (`afwa_diag_opt = 1`) when present in `wrfout` and falls back to a
   $C^0$-continuous WMO / Shuell plateau reduction (`tv_sl` capped smoothly at
   `290.5 K` when `tv_0 <= 290.5 K` with zero discontinuity across temperature branches).
5. **Signed heat fluxes & rainfall buckets (`core/post.py`):** Stock WRF v4.8
   outputs signed `ACHFX` and `ACLHF` without integer bucket counters (`I_ACHFX`/`I_ACLHF`
   do not exist in `Registry.EM_COMMON`), so `post_frame` differences signed `ACHFX`
   and `ACLHF` directly across `(t_prev, t_curr]` while retaining `I_RAINC` and
   `I_RAINNC` (`bucket_mm = 100.0`) for cumulative precipitation.
6. **CAPE provenance (`afwa_buoy_opt = 1`):** Enabling `afwa_buoy_opt = 1` in `&afwa`
   ([`registry.afwa`](https://github.com/wrf-model/WRF/blob/v4.8.0/Registry/registry.afwa))
   computes `AFWA_CAPE` and `AFWA_CAPE_MU` directly in `module_diag_afwa.F` without
   requiring Thompson aerosol-aware graupel number concentration; `wrf_nc.py`
   reads `AFWA_CAPE_MU` when present and falls back to `AFWA_CAPE`.

## 5. Comparison Against Dr. Jack Glendening (RASP / BLIPMAP) & Operational Caveats

- **Surface parcel excess (`T2` vs. RASP fixed $\Delta T$):** Revised MM5
  (`sf_sfclay_physics = 1`) diagnoses `T2` along the Monin–Obukhov profile between
  `TSK` and `temp_k[0]`, so `windy-av-layer` uses `parcelSurfaceDeltaTC = 0 K`.
- **Topographic radiation (`slope_rad = 1`, `topo_shading = 1`) vs. RASP `sfcsun`:**
  3 km slope aspect and `25 km` cast shadows enter Noah-MP's surface energy budget
  directly (`radt = 3 min`) without consumer double-counting.
- **Cold-start land state, snow, & high-altitude inland lakes:** Phase 1
  initializes Noah-MP (`5, 25, 70, 150 cm`) from `18Z` GFS (`6 h` spin-up).
