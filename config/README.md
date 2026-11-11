# Declarative Configuration (`config/`)

Sole owner of regional products, domains, and profiles bound into immutable
`RunLock` records during `av-wrf plan`.

```sh
uv run av-wrf check
```

- `products/*.yaml`: binds one domain, science, primary/candidate forcing, and
  output profile with explicit schedule and remaining-horizon policy (all active
  products are auto-listed in [`docs/reference.md`](../docs/reference.md)).
- `profiles/domains/*.yaml`: computational and published spherical Lambert
  geometry, vertical stretching, `terrain_elevations_m` checkpoints, and
  `geog_data_path` (mount WPS static geography at `/opt/av-wrf/geog`).
- `profiles/science/*.yaml`: frozen WRF v4.8 physics and dynamics parameters.
- `profiles/sources/*.yaml`: upstream forcing inventory, mirrors, soil centers,
  and dissemination lag.
- `profiles/outputs/*.yaml`: lead hours, interval transitions, and Zarr v3
  `sharding_indexed` tile/time-step policy.

Unknown keys, templating syntax, and environment interpolation are rejected at
parse time (`contracts/config.py`), and `scripts/check_repo.py` rejects any
region or product identifier hardcoded outside `config/`. Run
`python3 scripts/generate_reference.py` after editing any profile.
