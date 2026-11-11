# `av_wrf.codecs` (`L2`)

Local format serialization between `wrfout` NetCDF4 files and complete
`wrf-zarr-v3` sharded store hierarchies.

```python
from av_wrf.codecs import build_zarr_v3_store_metadata, zarr_shard_relpath
```

- **Allowed imports:** `av_wrf.contracts`, `av_wrf.core`, local `netCDF4`/`zarr`
  handles, and Python standard library.
- **Forbidden:** HTTP/S3 network clients, retries, `mpirun`, `os.environ`, and
  inline thermodynamic or wind-rotation formulas.
