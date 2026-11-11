# `av_wrf.cli` (`L4`)

Command-line argument router and the repository's sole `os.environ` reader.

```sh
uv run av-wrf check --product wrf-armenia-3km
```

- **Allowed imports:** `av_wrf.contracts`, `av_wrf.runner`, `av_wrf.verify`,
  and Python CLI/env stdlib modules (`argparse`, `os` in `env.py` only).
- **Forbidden:** physics math, file codecs, retry loops, or storage queries.
