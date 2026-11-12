# Generated Reference

Generated from code schemas and `config/`. Regenerate after schema or profile edits:

```sh
python3 scripts/generate_reference.py
```

## CLI Subcommands

Source: [`src/av_wrf/cli/main.py`](../src/av_wrf/cli/main.py).

- `av-wrf check`: Validate configured product(s), vertical stretch, namelist, and Zarr v3.
- `av-wrf plan`: Resolve cycle timestamps and persist an immutable run.lock.json.
- `av-wrf geog`: Build and validate geo_em.d01.nc in a new working directory.
- `av-wrf prep`: Write Fortran namelist.wps + namelist.input and forcing_subset.json recipe.
- `av-wrf forcing-manifest`: Verify local GRIB2 inputs and record the forcing inventory.
- `av-wrf solve`: Execute WPS ungrib/metgrid + MPI real/wrf and bind solver_receipt.json.
- `av-wrf post`: Stage wrfout frames into attempt-scoped sharded wrf-zarr-v3 arrays.
- `av-wrf publish`: Verify full RunLock horizon, promote Zarr store, and CAS latest.json.
- `av-wrf reconcile`: Dispatch pending _ready.json webhook idempotently (fail closed).

## Configured Products

Source: [`config/products/`](../config/README.md).
The effective remaining-horizon floor also reflects `max_cycle_age_hours`.
The configured `min_horizon_hours` sets the lower bound.

- `wrf-armenia-3km`: published grid `180 x 148` (`x0M = -268500.0`, `y0M = -220500.0`), primary `noaa-gfs-0p25-v1` (candidate `ecmwf-ifs-0p1-v1`, no solve recipe), `40` leads, `17` BL levels (`0-2 km` AGL), remaining-horizon floor `42 h`, `time_step = 15 s`.
- `wrf-armenia-pilot-3km-dwd`: published grid `84 x 84` (`x0M = -124500.0`, `y0M = -124500.0`), primary `dwd-icon-eu-v1` (candidate `noaa-gfs-0p25-v1`, recipe available), `16` leads, `17` BL levels (`0-2 km` AGL), remaining-horizon floor `10 h`, `time_step = 15 s`.
- `wrf-armenia-pilot-3km`: published grid `84 x 84` (`x0M = -124500.0`, `y0M = -124500.0`), primary `noaa-gfs-0p25-v1` (candidate `ecmwf-ifs-0p1-v1`, no solve recipe), `16` leads, `17` BL levels (`0-2 km` AGL), remaining-horizon floor `10 h`, `time_step = 15 s`.

## Wire Schema `wrf-zarr-v3` (v1, `provisional-phase-1`)

Source: [`zarr_schema.py`](../src/av_wrf/contracts/zarr_schema.py) and [`wrf-zarr-v3.schema.json`](../vendor/producer-contract/wrf-zarr-v3.schema.json).

- Spherical radius: `6370000.0` m; exported levels ceiling: `40`; moisture source of truth: `spfh_kgkg`; default inner chunk: `[64, 64]`; zstd level: `3`.

| Field | Dimensions | Units | `ModelLevelFrame` Target | Time Support | `keepbits` |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `terrain_m` | `(y, x)` | `m` | `terrainM` | `static` | `17` |
| `surface_pressure_pa` | `(time, y, x)` | `Pa` | `surfacePressurePa` | `instant` | `17` |
| `temp_2m_k` | `(time, y, x)` | `K` | `temp2mK` | `instant` | `15` |
| `spfh_2m_kgkg` | `(time, y, x)` | `kg/kg` | `specificHumidity2mKgKg` | `instant` | `15` |
| `sensible_heat_flux_wm2` | `(time, y, x)` | `W/m2` | `sensibleHeatFluxWm2` | `window-mean` | `15` |
| `latent_heat_flux_wm2` | `(time, y, x)` | `W/m2` | `latentHeatFluxWm2` | `window-mean` | `15` |
| `precip_mm_hr` | `(time, y, x)` | `mm/h` | `precipitationMmPerHour` | `window-rate` | `15` |
| `mslp_pa` | `(time, y, x)` | `Pa` | `auxSurface.mslpPa` | `instant` | `17` |
| `u_10m_earth_mps` | `(time, y, x)` | `m/s` | `auxSurface.u10mMps` | `instant` | `15` |
| `v_10m_earth_mps` | `(time, y, x)` | `m/s` | `auxSurface.v10mMps` | `instant` | `15` |
| `gust_10m_mps` | `(time, y, x)` | `m/s` | `auxSurface.gust10mMps` | `instant-parameterized` | `15` |
| `pbl_height_m` | `(time, y, x)` | `m` | `auxSurface.pblHeightM` | `instant` | `15` |
| `cape_jkg` | `(time, y, x)` | `J/kg` | `auxSurface.capeJkg` | `instant` | `15` |
| `cloud_total_pct` | `(time, y, x)` | `%` | `auxSurface.cloudTotalPct` | `instant` | `15` |
| `cloud_low_pct` | `(time, y, x)` | `%` | `auxSurface.cloudLowPct` | `instant` | `15` |
| `pressure_pa` | `(time, level, y, x)` | `Pa` | `pressurePa` | `instant` | `17` |
| `height_msl_m` | `(time, level, y, x)` | `m` | `hgtM` | `instant` | `17` |
| `temp_k` | `(time, level, y, x)` | `K` | `tempK` | `instant` | `15` |
| `spfh_kgkg` | `(time, level, y, x)` | `kg/kg` | `auxLevels.spfhKgKg` | `instant` | `15` |
| `u_earth_mps` | `(time, level, y, x)` | `m/s` | `auxLevels.uWindMps` | `instant` | `15` |
| `v_earth_mps` | `(time, level, y, x)` | `m/s` | `auxLevels.vWindMps` | `instant` | `15` |
| `omega_pas` | `(time, level, y, x)` | `Pa/s` | `auxLevels.omegaPaS` | `instant` | `15` |
