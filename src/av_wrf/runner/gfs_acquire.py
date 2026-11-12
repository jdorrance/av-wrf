"""Acquire validated GFS boundaries from configured mirrors or NOAA's filter."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from datetime import datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory, gettempdir
from time import sleep
from typing import Literal

import httpx

from av_wrf.runner.bundle import LoadedBundle, load_forcing_plan
from av_wrf.runner.forcing_gfs import GFS
from av_wrf.runner.http_request import _request
from av_wrf.runner.mirrors import ForcingSubset
from av_wrf.runner.range_fetch import fetch_ranges
from av_wrf.runner.store import LocalFileStore, json_bytes, put_idempotent, sha256_file

_FILTER_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
type _FetchMode = Literal["ranges", "regional", "fallback"]


def _filter_params(bounds: list[float], key: PurePosixPath) -> dict[str, str]:
    west, south, east, north = bounds
    # Keep optional WPS fields such as snow depth when requesting the regional subset.
    return {
        "file": key.name,
        "dir": f"/{key.parent}",
        "all_var": "on",
        "all_lev": "on",
        "subregion": "",
        "leftlon": str(west),
        "bottomlat": str(south),
        "rightlon": str(east),
        "toplat": str(north),
    }


def _cgi_fetch(client: httpx.Client, bounds: list[float], key: PurePosixPath) -> bytes:
    lock = Path(gettempdir()) / "av-wrf-noaa-filter.lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Hold the host-wide lock through the required interval, including failures.
        # https://nomads.ncep.noaa.gov/info.php?page=gribfilter
        return _request(
            client,
            "GET",
            _FILTER_URL,
            params=_filter_params(bounds, key),
            after_attempt=lambda: sleep(10),
        ).content
    finally:
        os.close(fd)


def _fetch_validated(
    client: httpx.Client,
    bundle: LoadedBundle,
    cycle: datetime,
    plan: ForcingSubset,
    key: str,
    hour: int,
    incoming: Path,
    mode: _FetchMode,
) -> bytes:
    if mode != "regional":
        errors: list[str] = []
        for mirror in bundle.primary_source.mirrors:
            try:
                data = fetch_ranges(client, (mirror,), key)
                incoming.write_bytes(data)
                GFS.validate_file(incoming, cycle, bundle.primary_source, bundle.domain, hour)
                return data
            except (ValueError, httpx.HTTPError) as exc:
                errors.append(f"{mirror}: {exc}")
        if mode == "ranges":
            raise ValueError("All GFS mirrors failed admission: " + "; ".join(errors))
    data = _cgi_fetch(client, plan["bbox_wsen_deg"], PurePosixPath(key))
    incoming.write_bytes(data)
    GFS.validate_file(incoming, cycle, bundle.primary_source, bundle.domain, hour)
    return data


def _acquire(
    client: httpx.Client,
    bundle: LoadedBundle,
    cycle: datetime,
    plan: ForcingSubset,
    grib_dir: Path,
    through_hour: int | None,
    *,
    mode: _FetchMode = "ranges",
) -> dict[int, str]:
    hours = [
        hour for hour in plan["forecast_hours"] if through_hour is None or hour <= through_hour
    ]
    if not hours:
        raise ValueError("--through-hour precedes the first required boundary hour")
    destination = LocalFileStore(grib_dir)
    files: dict[int, str] = {}
    source_sha256s: dict[str, str] = {}
    normalized_sha256s: dict[str, str] = {}
    with TemporaryDirectory() as tmp:
        incoming = Path(tmp) / "gfs.grib2"
        for hour in hours:
            key = plan["file_keys"][str(hour)]
            if not isinstance(key, str):
                raise ValueError("Expected one NOAA GFS key per boundary hour")
            object_path = PurePosixPath(key)
            name = object_path.name
            target = grib_dir / name
            if target.is_symlink():
                raise ValueError(f"Expected a regular GFS file at {target}")
            if target.exists():
                GFS.validate_file(target, cycle, bundle.primary_source, bundle.domain, hour)
                digest = sha256_file(target)
            else:
                data = _fetch_validated(client, bundle, cycle, plan, key, hour, incoming, mode)
                digest = put_idempotent(destination, name, data)
            files[hour] = name
            source_sha256s[key] = normalized_sha256s[name] = digest
    inventory = {
        "forcing_cycle_utc": cycle.isoformat(),
        "source_sha256s": source_sha256s,
        "normalized_sha256s": normalized_sha256s,
    }
    put_idempotent(
        destination, f"gfs.{cycle:%Y%m%d%H}.sources-f{hours[-1]:03d}.json", json_bytes(inventory)
    )
    return files


def main(argv: list[str] | None = None) -> int:
    """Print the verified hour-to-filename inventory for forcing-manifest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True, help="Path to forcing_subset.json")
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--grib-dir", type=Path, required=True, help="Write validated GRIB2 here")
    parser.add_argument("--through-hour", type=int, help="Last boundary lead for a smoke run")
    fetch_mode = parser.add_mutually_exclusive_group()
    fetch_mode.add_argument(
        "--fallback-cgi", action="store_true", help="Use NOAA filter after mirrors fail"
    )
    fetch_mode.add_argument(
        "--regional-filter", action="store_true", help="Fetch the planned cutout from NOAA"
    )
    args = parser.parse_args(argv)
    bundle, cycle, plan = load_forcing_plan(args.plan, args.config_dir, GFS)
    with httpx.Client(timeout=120) as client:
        files = _acquire(
            client,
            bundle,
            cycle,
            plan,
            args.grib_dir,
            args.through_hour,
            mode="regional"
            if args.regional_filter
            else "fallback"
            if args.fallback_cgi
            else "ranges",
        )
    print(json.dumps(files))
    return 0


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
