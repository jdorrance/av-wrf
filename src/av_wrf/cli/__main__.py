"""Module execution entrypoint (`python3 -m av_wrf.cli`)."""

from __future__ import annotations

from av_wrf.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
