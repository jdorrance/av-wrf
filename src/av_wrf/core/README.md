# `av_wrf.core` (`L1`)

Pure deterministic spherical Lambert geometry, schedule/horizon math, vertical
level checks, WRF v4.8 namelists, soil interpolation, `post_frame` physics, and
soaring column metrics.

```python
from av_wrf.core import (
    geometric_w_to_omega_pas,
    published_lambert_grid_metadata,
    resolve_cycle_window,
    rotate_grid_to_earth,
)
```

- **Allowed imports:** `av_wrf.contracts` and Python math/datetime/struct stdlib
  (`core.*` peers may import `core.grid` only).
- **Forbidden:** file/network I/O, `os`, `sys`, `subprocess`, `netCDF4`, `zarr`,
  `random`, and wall-clock calls (`datetime.now`, `time.time`).
