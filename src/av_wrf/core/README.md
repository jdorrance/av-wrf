# `av_wrf.core` (`L1`)

Pure deterministic spherical Lambert geometry, schedule/horizon math, vertical
level checks, WRF v4.8 namelists, `post_frame` physics, and spin-up metrics.

```python
from av_wrf.core import (
    post_frame,
    published_lambert_grid_metadata,
    resolve_cycle_window,
)
```

- **Allowed imports:** `av_wrf.contracts`, NumPy and Python math/datetime/struct stdlib
  (`core.*` peers may import `core.grid`, `core.thermo`, and `core.vertical`).
- **Forbidden:** file/network I/O, `os`, `sys`, `subprocess`, `netCDF4`, `zarr`,
  `random`, and wall-clock calls (`datetime.now`, `time.time`).
