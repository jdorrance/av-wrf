# Solver patches

This directory holds optional `wrf-*.patch` and `wps-*.patch` inputs. The current
build applies zero standalone patches. The WRF Makefile dependency fix lives
in [`build-wrf.sh`](../build-wrf.sh).

[`manifest.lock`](../../manifest.lock) records the directory contents. The
packaged `build-manifest.json` records the source commits and applied patch hashes.
