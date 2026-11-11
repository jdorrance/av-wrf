# Producer Wire Contract (`vendor/producer-contract/`)

Provisional Phase 1 `wrf-zarr-v3` wire schema locked by SHA-256 in
[`manifest.lock`](../../manifest.lock). Once `windy-av-layer` lands its canonical
copy at `packages/contracts/schemas/wrf-zarr-v3.schema.json`, sync and verify via:

```sh
python3 scripts/sync_contract.py --from-consumer ../windy-av-layer
python3 scripts/sync_contract.py --verify
```

`av_wrf.contracts.zarr_schema` mirrors [`wrf-zarr-v3.schema.json`](wrf-zarr-v3.schema.json)
1:1 (`tests/contract/test_zarr_schema_sync.py`).
