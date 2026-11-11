# Post-Release Solver Patches (`docker/patches/`)

Audited `WRF v4.8.0` / `WPS v4.7.0` stability patches applied deterministically
during `docker/Dockerfile` builds (e.g., `#2300` restart uninitialized-memory
fix and `#2292` `auxinput` handle cleanup).

Every file in this directory is hashed in [`manifest.lock`](../../manifest.lock).
Add or update patches via `python3 scripts/sync_contract.py --update-locks`.
