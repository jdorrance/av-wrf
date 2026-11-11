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
| $R_d$ ($\text{J kg}^{-1}\text{ K}^{-1}$) | `287.0` ([`module_model_constants.F`](https://github.com/wrf-model/WRF/blob/master/share/module_model_constants.F)) | `287.05` (WMO) | Exact inversion of WRF `T` / `THM` without upper-air drift |
| $c_{pd}$ ($\text{J kg}^{-1}\text{ K}^{-1}$) | `1004.5` ($= \frac{7}{2} R_d$, $\kappa = \frac{2}{7}$ exact) | `1004.0` ($\kappa = 0.2859$, mixed-layer `0.2854`) | Matches WRF RK3 equation of state to machine precision |
| $R_v$ ($\text{J kg}^{-1}\text{ K}^{-1}$) | `461.6` ($R_v / R_d = 1.608362$, $\delta = 0.608362$) | $\epsilon = 0.622 \implies \delta = 0.607717$ | Matches WRF `THM = \theta(1 + (R_v/R_d)q_v) - T_0` |
| $L_v$ ($\text{J kg}^{-1}$) | `2,500,000` (`xlv` in WRF) | `2,501,000` (Bolton 1980) | Used in `av-wrf` only for convective gust buoyancy |
| Gravity $g$ ($\text{m s}^{-2}$) | `9.81` (`g` in WRF) | `9.80665` (WMO $g_0$) | Preserves `(PH + PHB) / 9.81 == HGT` at ground ($k_w = 0$) |
| Earth radius $R_\oplus$ ($\text{m}$) | `6,370,000` (`reradius` in WPS/WRF) | `6,370,000` (passed via `LambertConformalGrid`) | Single-owner Lambert descriptor eliminates projection drift |

- **Why `(PH + PHB) / 9.81` must not use `9.80665`:** WRF initializes base-state
  geopotential at the surface face as $\text{PHB}(k_w = 0) = 9.81 \cdot \text{HGT}$.
  Dividing by `9.80665` over a `4,100 m` massif would introduce a `+1.40 m`
  ground-datum offset relative to `terrain_m` (`HGT`), distorting near-surface
  `heightM - terrainM` AGL differences in `windy-av-layer`.
- **Vertical geoid datum (`EGM2008`):** Both `GMTED2010` / `Copernicus DEM DGED 2023_1 GLO-30`
  and `windy-av-layer` (`Copernicus GLO-90`) express orthometric elevations on
  `EGM2008` with zero vertical-datum transformation required across the boundary.

## 2. Temperature, Moisture, and Consumer Saturation Guards

1. **Sensible air temperature (`T` vs. `THM`):** Even with moist potential
   temperature dynamics (`use_theta_m = 1`, [Skamarock et al. 2019](https://www2.mmm.ucar.edu/wrf/users/docs/technote/v4_technote.pdf)
   Eq. 2.11), `wrfout` `T` stores dry perturbation potential temperature
   $\theta - T_0$ ($T_0 = \text{base\_temp} = 300\text{ K}$), while `THM` stores
   $\theta_m - T_0 = \theta(1 + (R_v/R_d)q_v) - T_0$ ([WRF Users' Guide Ch. 5](https://www2.mmm.ucar.edu/wrf/users/docs/user_guide_v4/v4.4/users_guide_chap5.html),
   [WRF Forum #5569](https://forum.mmm.ucar.edu/threads/accessing-variables-on-head_grid.5569/)).
   `codecs/wrf_nc.py` cross-checks `potential_to_temp_k(T)` against
   `moist_thm_to_temp_k(THM, QVAPOR)` on every ingested `wrfout` slice so any
   moist/dry confusion fails closed immediately.
2. **Specific humidity & omission of `rhPercent`:** WRF `QVAPOR` and `Q2` are
   water-vapor mixing ratios $r$ (`kg/kg dry air`), converted via $q = r / (1 + r)$.
   Model-level `rhPercent` is intentionally omitted because `windy-av-layer`
   (`packages/forecast/src/native-fields.ts`) computes dewpoint directly from
   `spfhKgKg` and `pressurePa`, matching `ICON-EU` and `RRFS`.
3. **2 m Bolton liquid-saturation guard (`clamp_spfh_2m_to_liquid_sat`):** WRF's
   Revised MM5 surface layer (`sf_sfclay_physics = 1`) diagnoses `Q2` from
   Monin–Obukhov similarity without liquid-water saturation adjustment, so `Q2`
   can exceed `105%` RH WRT liquid water in nocturnal radiation or freezing fog.
   Because `windy-av-layer` (`column-input.ts` / `column.ts:169`) clamps only up to
   `1.05 * e_s(T_2)` and marks columns above `1.05 * e_s` as `STATUS_UNAVAILABLE`,
   `av-wrf` caps `spfh_2m_kgkg` at consumer Bolton (1980) liquid saturation
   ($e_s = 6.112 \exp(17.67 T_c / (T_c + 243.5))\text{ hPa}$, $\epsilon = 0.622$)
   with $2^{-14}$ mantissa headroom so `keepbits = 15` rounding never exceeds `100%` RH.

## 3. Vertical Coordinate, Mountain Compression, & Consumer `1,000 m` Gap Limit

1. **Log-pressure stretching & cubic hybrid coordinate (`core/vertical.py`):**
   Following [Skamarock et al. (2019, Eqs. 2.2–2.6 & 5.3–5.7)](https://www2.mmm.ucar.edu/wrf/users/docs/technote/v4_technote.pdf)
   and [WRF Real-Data Initialization](https://www2.mmm.ucar.edu/wrf/site/users_guide/real_initialization.html),
   `auto_levels_opt = 2` stretches domain-wide $\eta \in [1, 0]$ levels in
   log-pressure (`p0 = 100 kPa`, `T0 = 290 K`, `A = 50 K`, `T_min = 200 K`), and
   `hybrid_opt = 2` (`etac = 0.2`) maps them over elevated terrain via:
   $$p_d(\eta) = B(\eta)(\bar{p}_s - p_t) + [\eta - B(\eta)] \, (p_0 - p_t) + p_t$$
   where $B(\eta)$ is the cubic satisfying $B(1) = 1$, $B'(1) = 1$, $B(\eta_c) = 0$,
   and $B'(\eta_c) = 0$. Because $\max B'(\eta) \approx 1.681 > 1$ at $\eta \approx 0.69$
   for $\eta_c = 0.2$, lower-to-mid-tropospheric layers **compress** over high peaks
   (`20` BL levels in `0–2 km` AGL at `800 m` basins; `24+` levels over `4–5 km`
   summits) and would fold ($\partial p_d / \partial \eta \le 0$) if $\eta_c \gtrsim 0.32$
   over `5 km` peaks. `av-wrf check` verifies monotonic $\Delta p_d > 0$ across
   every elevation listed in `DomainProfile.terrain_elevations_m`.
2. **Summer thermal-expansion headroom (`max_dz_m <= 900 m` vs. `1,000 m`):**
   `windy-av-layer` (`packages/soaring/src/column.ts:159`) rejects an **entire
   column as `unavailable`** if any adjacent level gap exceeds `maximumProfileGapM = 1,000 m`.
   Because extreme summer heatwaves ($T \approx 1.08 \, T_{\text{ref}}$) thermally
   expand hydrostatic layer thicknesses above WRF's `290 K` reference profile,
   `vertical.py` enforces `max_dz_m <= 900 m` (`850 m` in shipped domain profiles),
   and `runner/post.py` verifies actual exported `height_msl_m - terrain_m` gaps.
3. **First mass midpoint vs. consumer `minimumLevelAglM = 20 m`:** With
   `dzbot_m = 34 m` ($\ge 25\text{ m}$ coupled surface-layer stability floor per
   [WRF Forum #8325](https://forum.mmm.ucar.edu/threads/increase-eta-level-and-decrease-dzbot.8325/)),
   the lowest mass midpoint sits at `~15–17 m` AGL. Consumer map-layer `column.ts`
   evaluates `k = 0` directly, whereas Windgrams-style `native.ts` screens levels
   $\le 20\text{ m}$ AGL and starts at `k = 1` (`~45–52 m` AGL) with `19+` BL levels
   remaining above `20 m` AGL.

## 4. Winds, Vertical Velocity, MSLP, Gusts, Clouds, and CAPE

1. **Grid-to-Earth wind rotation & Lambert CFL (`core/grid.py`, `core/namelist.py`):**
   WRF `SINALPHA = -sin(theta_lambert)` where $\theta = n(\lambda - \lambda_0)$
   ([`wrf-python` #142](https://github.com/NCAR/wrf-python/issues/142)), verified
   analytically in both hemispheres. `namelist.py` evaluates maximum Lambert map
   scale $m_{\max}$ across domain latitudes to enforce the physical CFL bound
   $\Delta t \le 6 \min(\Delta x, \Delta y) / m_{\max}$ (Skamarock et al. 2019 Sec. 3.3.4).
2. **Hydrostatic $\omega$ round-trip (`geometric_w_to_omega_pas`):** Nonhydrostatic
   WRF outputs geometric $w = dz/dt$ (`m/s`). `av-wrf` intentionally converts via
   hydrostatic $\omega = -\rho_m g w$ (omitting $\partial p'/\partial t + \mathbf{v}_h \cdot \nabla_z p'$)
   because `windy-av-layer` (`geometricWFromOmegaMps`) inverts `omegaPaS` back to
   geometric $w$ hydrostatically for wave/windgram display; adding horizontal
   pressure advection would corrupt mountain-wave $w$ on inversion.
3. **10 m wind gusts over high terrain (`parameterized_gust_10m_mps`):** WRF
   `WSPD10MAX` (`nwp_diagnostics = 1`) tracks the maximum **3 km grid-cell mean**
   10 m wind across solver steps (plus AFWA heavy-rain blending only above `50 mm/h`),
   missing subgrid mechanical and thermal gusts. `av-wrf` takes
   $\max(\text{WSPD10MAX}, V_{10} + \Delta V_{\text{turb}})$ using the
   [Panofsky et al. (1977)](https://www.pmf.unizg.hr/_download/repository/tb1_ch2.pdf) /
   [ECMWF IFS `10fg`](https://codes.ecmwf.int/grib/param-db/49) turbulent-convective
   increment $\Delta V_{\text{turb}} = 7.71 (u_*^3 + \frac{0.5 \kappa}{12} w_*^3)^{1/3}$
   with **actual high-terrain surface density** $\rho_{\text{sfc}} = p_{\text{sfc}} / (R_d T_{v0})$
   and moist virtual buoyancy flux (`H` + `LE`, matching `windy-av-layer` `column.ts`
   and Dr. Jack Glendening's RASP `vhf`). ECMWF's baric downdraft shear term
   $\max(0, U_{850} - U_{950})$ ([Bechtold & Bidlot 2009](https://www.ecmwf.int/en/elibrary/80227-parametrization-convective-gusts))
   is intentionally omitted because `850` and `950 hPa` lie underground across
   elevated plateaus while 3 km WRF resolves cold-pool outflows in `WSPD10MAX`.
4. **Plateau MSLP (`hypsometric_mslp_pa`):** Uses the WMO / WRF Shuell reduction
   ([CORDEX-WRF GMD 2019](https://gmd.copernicus.org/articles/12/1029/2019/)
   `psl_diag = 1`), clamping extrapolated sea-level virtual temperatures at
   `290.5 K` on hot summer plateaus and damping `< 255 K` winter basin inversions.
5. **Cloud overlap & low-cloud $\sigma \ge 0.8$ (`core/post.py`):** Applies
   Geleyn & Hollingsworth / [Hogan & Illingworth (2000)](https://journals.ametsoc.org/view/journals/atsc/72/8/jas-d-14-0278.1.xml)
   maximum-random overlap to 3D `CLDFRA`. `cloud_low_pct` filters `p >= 0.8 * PSFC`
   (ECMWF terrain-following $\sigma \ge 0.8$) rather than ICON's fixed `800 hPa`
   threshold, which lies underground above `1,950 m` MSL.
6. **CAPE provenance:** `cape_jkg` exports `AFWA_CAPE` (`phys/module_diag_afwa.F`),
   defined as undiluted virtual-temperature `MUCAPE` from the most-unstable parcel
   in the lowest `~300 hPa` AGL (declared in `wrf-zarr-v3.schema.json`).

## 5. Comparison Against Dr. Jack Glendening (RASP / BLIPMAP) & Operational Caveats

- **Surface parcel excess (`T2` vs. RASP fixed $\Delta T$):** Revised MM5
  (`sf_sfclay_physics = 1`) diagnoses `T2` along the Monin–Obukhov similarity
  profile between hot skin `TSK` and `temp_k[0]`, naturally supplying `1–2.5 K`
  superadiabatic excess on sunny afternoons so `windy-av-layer` uses
  `parcelSurfaceDeltaTC = 0 K`.
- **Topographic radiation (`slope_rad = 1`, `topo_shading = 1`) vs. RASP `sfcsun`:**
  3 km slope aspect and `25 km` cast shadows enter Noah-MP's surface energy budget
  directly (`radt = 3 min`); `windy-av-layer` interpolates donor heat fluxes without
  re-scaling by DEM slope aspect, preventing double-counting.
- **Trailing flux windows & 3-hourly tail (`f039..f048`):** Trailing 1 h mean
  fluxes (`f001..f036`) damp transient cloud-shadow noise compared to RASP's
  instantaneous `HFX`. For a `00Z` cycle in a `UTC+4` timezone, `f036` is `16:00`
  local on Day 2, so late-afternoon glass-off (`16:00–19:00` local) falls in the
  first 3-hourly window (`f039`); `hourly_through_hour` is a pure output-profile
  YAML knob when finer Day-2 evening cadence is needed.
- **Cold-start land state, snow, & high-altitude inland lakes:** Phase 1
  initializes Noah-MP (`5, 25, 70, 150 cm`) from `18Z` GFS (`6 h` spin-up).
  Coarse GFS snow depth (`SNOD`) and skin temperature over high-altitude lakes
  can bias spring snow-breeze and lake-breeze convergence until `IMS 1 km` snow
  and `OSTIA` lake temperatures are benchmarked in Phase 5.
