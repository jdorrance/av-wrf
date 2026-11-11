# `av_wrf.runner` (`L3a`)

Effectful runtime adapters for object storage (`store.py`), declarative PyYAML
bundle loading (`bundle.py`), stage validation and namelist/forcing-subset
`prep` (`stages.py`), attempt-staged `post` (`post.py`), full-horizon `publish`
promotion (`publish.py`), container `BuildManifest` verification (`solver.py`),
forcing bundles (`mirrors.py`), and fail-closed webhook dispatch (`dispatch.py`).

```python
from av_wrf.runner import LocalFileStore, ObjectStore, check_all_products
```

- **Allowed imports:** `av_wrf.contracts`, `av_wrf.core`, `av_wrf.codecs`, and I/O libraries.
- **Forbidden:** importing `av_wrf.verify` or `av_wrf.cli`, reading `os.environ`,
  or implementing inline thermodynamic/projection math.
