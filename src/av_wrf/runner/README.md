# `av_wrf.runner` (`L3a`)

Effectful runtime adapters for object storage (`store.py`), declarative PyYAML
bundle loading (`bundle.py`), stage validation and namelist/forcing-subset
`prep` (`stages.py`), attempt-staged `post` (`post.py`), full-horizon `publish`
promotion (`publish.py`), container `BuildManifest` verification (`solver.py`),
forcing bundles (`mirrors.py`), and fail-closed webhook dispatch (`dispatch.py`).

```python
from av_wrf.runner import LocalFileStore, ObjectStore, check_all_products
```

Source recipes in [`mirrors.py`](mirrors.py) define acquisition, GRIB2 admission,
and WPS inputs. `prep` emits the cycle's footprint and required source keys.
Acquire into a per-cycle directory using the appropriate prep output.

```sh
uv run python -m av_wrf.runner.gfs_acquire \
  --plan work/prep/forcing_subset.json --grib-dir work/grib
uv run --extra icon python -m av_wrf.runner.icon_acquire \
  --plan work/prep/forcing_subset.json --cache work/dwd --grib-dir work/grib
```

The GFS command needs `grib_ls` and hashes the received NOAA regional subset.
Stage archived inputs directly when available. Seal the complete cycle with
`forcing-manifest --file HOUR=FILENAME`. Use `--acquisition-inventory` to bind
the generated `*.sources-*.json` to the manifest and solver receipt. Use fresh
directories to acquire revised bytes.

Solve validates private input copies and records native stage wall seconds in
its receipt. Set `AV_WRF_NATIVE_TIMEOUT_S` to bound each native stage. Failed
attempts retain their logs in scratch.

- **Allowed imports:** `av_wrf.contracts`, `av_wrf.core`, `av_wrf.codecs`, and I/O libraries.
- **Forbidden:** importing `av_wrf.cli`, reading `os.environ`,
  or implementing inline thermodynamic/projection math.
