# Declarative Configuration (`config/`)

Pure data profiles bound into immutable `RunLock` records during `av-wrf plan`.

```sh
uv run av-wrf check --product wrf-armenia-3km
```

- [`products/`](products/wrf-armenia-3km.yaml): binds one domain, science,
  primary forcing, and output profile with explicit schedule and horizon policy.
- `profiles/domains/`: computational and published spherical Lambert geometry
  plus vertical stretching parameters.
- `profiles/science/`: frozen WRF v4.8 physics and dynamics namelist parameters.
- `profiles/sources/`: upstream forcing inventory, mirrors, soil centers, and
  expected dissemination lag.
- `profiles/outputs/`: lead hours, interval changes, and Zarr v3 shard policy.

Unknown keys, templating syntax, and environment interpolation are rejected at
parse time (`contracts/config.py`). Run `python3 scripts/generate_reference.py`
after editing any profile.
