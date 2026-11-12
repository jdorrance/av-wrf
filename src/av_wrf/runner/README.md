# `av_wrf.runner` (`L3a`)

Effectful runtime adapters for object storage (`store.py`), declarative PyYAML
bundle loading (`bundle.py`), stage validation and namelist/forcing-subset
`prep` (`stages.py`), attempt-staged `post` (`post.py`), full-horizon `publish`
promotion (`publish.py`), container `BuildManifest` verification (`solver.py`),
forcing bundles (`mirrors.py`), and fail-closed webhook dispatch (`dispatch.py`).

```python
from av_wrf.runner import LocalFileStore, ObjectStore, check_all_products
```

Register a source recipe in [`mirrors.py`](mirrors.py) and select its feed in a
source profile. The recipe plans acquisition, checks the forcing window and
footprint, verifies assembled GRIB2 files, and declares its WPS table and
metgrid inputs. `prep` emits mirror-relative `file_keys` by boundary hour. A key
can be a string or an ordered list of objects to assemble. Hours may reference
the same invariant object. `forcing-manifest --file HOUR=FILENAME` seals one
normalized file for each hour before solve. See the
[ICON-EU exercise](../../../docs/review-plan.md#icon-eu-forcing-exercise).

- **Allowed imports:** `av_wrf.contracts`, `av_wrf.core`, `av_wrf.codecs`, and I/O libraries.
- **Forbidden:** importing `av_wrf.verify` or `av_wrf.cli`, reading `os.environ`,
  or implementing inline thermodynamic/projection math.
