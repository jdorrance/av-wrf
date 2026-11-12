# Solver patches

The solver applies the pinned source patches listed in its build manifest.

`wrf-noahmp-preserve-albedo.patch` backports the WRF adapter guard from
Noah-MP commit `57b9373`. It retains WRF's last valid broadband albedo when
Noah-MP returns an undefined or out-of-range value. RRTMG uses the retained
value at the next radiation call.

[Upstream fix](https://github.com/NCAR/noahmp/commit/57b937397ca6086ebf9b7b9072b1a7f0239db4b9)
[Noah-MP energy solver](https://github.com/NCAR/noahmp/blob/5da0b241e48ecfd9a2a1bd667ed554765856d589/src/EnergyMainMod.F90#L342-L347)
[WRF RRTMG](https://github.com/wrf-model/WRF/blob/v4.8.0/phys/module_ra_rrtmg_sw.F#L10964-L10975)

`wps-ungrib-packed-grib2.patch` enables ungrib's built-in GRIB2 templates in
the JPEG2000/PNG-free build and exits on unsupported field packing.

[WPS ungrib](https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/src/ungrib.F#L140-L155)
[WPS packing templates](https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/src/ngl/g2/gf_unpack7.F#L104-L117)

[`manifest.lock`](../../manifest.lock) pins these files.
