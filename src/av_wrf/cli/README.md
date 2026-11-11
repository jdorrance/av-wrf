# `av_wrf.cli` (`L4`)

Command-line argument router and the repository's sole `os.environ` reader.

```sh
uv run av-wrf check
```

- **Allowed imports:** `av_wrf.contracts`, `av_wrf.runner`, `av_wrf.verify`,
  and Python CLI/env stdlib modules (`argparse`, `os` in `env.py` only).
- **Forbidden:** hardcoded product/region defaults, physics math, file codecs,
  retry loops, or direct storage queries.
