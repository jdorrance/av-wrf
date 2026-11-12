# `av_wrf.verify` (`L3b`)

Compute boundary-layer and wind shear scores from a typed sounding.

```python
from av_wrf.verify import evaluate_column_soaring_proxies
```

- **Allowed imports:** `av_wrf.contracts`, `av_wrf.core`, `av_wrf.codecs`.
- **Forbidden:** importing `av_wrf.runner` or `av_wrf.cli`, executing `WRF`,
  mutating `latest.json`, or dispatching webhooks.
