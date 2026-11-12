# `av-wrf`

Regional `WRF-ARW` producer writing immutable native model-level Zarr v3 forecasts
and ready/diagnostic receipts for `windy-av-layer`.

Use Python 3.12 and run from the repository root:

```sh
uv sync --locked
uv run av-wrf check
export AV_WRF_STORE_URI=file:///tmp/av-wrf-store
uv run av-wrf plan --product <product-id> --cycle 2026-11-12T00:00:00Z --attempt att-01 --allow-unpinned-dev
uv run verify
```

[Configuration](config/README.md) binds domain, science, GFS/ICON-EU/IFS forcing,
and output profiles into `run.lock.json`. The [runner](src/av_wrf/runner/README.md)
acquires forcing, executes the solver, stages frames, and publishes complete
horizons to local or S3-compatible storage. Parcel physics, subgrid terrain,
sample packs, and tiles belong to `windy-av-layer`.

- [Architecture](docs/architecture.md): package map, import DAG, and invariants.
- [Science](docs/science.md): physical conventions and qualification limits.
- [Reference](docs/reference.md): generated commands, products, and field inventory.
- [Contract](vendor/producer-contract/README.md): provisional `wrf-zarr-v3` schema.
- [Containers](docker/README.md): runtime and pinned WRF/WPS solver builds.
- [Plan](docs/plan.md) and [review evidence](docs/review-plan.md): release gates and measurements.
- [AGENTS.md](AGENTS.md): repository rules; `python3 scripts/check_repo.py` checks structure.

License: [MIT](LICENSE). Upstream software and dataset notices are recorded in
[THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt).
