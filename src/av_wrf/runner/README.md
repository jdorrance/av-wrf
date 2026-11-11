# `av_wrf.runner` (`L3a`)

Effectful runtime adapters for object storage (`file://` and S3/R2), fail-closed
forcing downloads, container solver execution, stage wiring, and durable
webhook dispatch.

```python
from av_wrf.runner import LocalFileStore, ObjectStore, check_product_config
```

- **Allowed imports:** `av_wrf.contracts`, `av_wrf.core`, `av_wrf.codecs`, and I/O libraries.
- **Forbidden:** importing `av_wrf.verify` or `av_wrf.cli`, reading `os.environ`,
  or implementing inline thermodynamic/projection math.
