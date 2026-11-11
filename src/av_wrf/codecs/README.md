# `av_wrf.codecs` (`L2`)

Local format serialization between `Times`-indexed `wrfout` NetCDF4 files and
deterministic [ZEP-0002](https://zarr.dev/zeps/accepted/ZEP0002.html)
`sharding_indexed` (`zstd` + `crc32c`) Zarr v3 stores (`wrf_nc.py`,
`zarr_shard.py`, `zarr_v3.py`).

```python
from av_wrf.codecs import build_zarr_v3_store_metadata, encode_3d_shard_bytes
```

- **Allowed imports:** `av_wrf.contracts`, `av_wrf.core`, `zstandard`/`libzstd`,
  `numpy`, local `netCDF4`/`zarr` handles, and Python standard library.
- **Forbidden:** HTTP/S3 network clients, subprocesses, retries, `mpirun`,
  `os.environ`, and inline thermodynamic or wind-rotation formulas.
