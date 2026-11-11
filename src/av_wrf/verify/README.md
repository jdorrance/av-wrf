# `av_wrf.verify` (`L3b`)

Prospective input/baseline archiver, regional observation parsers, and
mature-lead NWP + soaring-proxy scorecards.

```python
from av_wrf.verify import evaluate_column_soaring_proxies
```

- **Allowed imports:** `av_wrf.contracts`, `av_wrf.core`, `av_wrf.codecs`.
- **Forbidden:** importing `av_wrf.runner` or `av_wrf.cli`, executing `WRF`,
  mutating `latest.json`, or dispatching webhooks.
