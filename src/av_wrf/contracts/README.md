# `av_wrf.contracts` (`L0`)

Frozen schemas, `RunLock` and receipt records, the 22-field `wrf-zarr-v3`
template, and nominal unit/stagger types.

```python
from av_wrf.contracts import WRF_ZARR_V3_FIELDS, DomainProfile, EarthWindMps
```

- **Allowed imports:** Python standard library only.
- **Forbidden:** array math, physics formulas, file/network I/O, `os.environ`,
  `netCDF4`, `zarr`, subprocesses, and mutable records.
