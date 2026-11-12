"""Fetch regional GFS boundaries from NOAA NOMADS for an existing prep plan."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from time import sleep

import httpx

from av_wrf.runner.bundle import LoadedBundle, load_forcing_plan
from av_wrf.runner.forcing_gfs import GFS
from av_wrf.runner.mirrors import ForcingSubset
from av_wrf.runner.store import LocalFileStore, json_bytes, put_idempotent, sha256_file

_FILTER_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"


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


def _acquire(
    client: httpx.Client,
    bundle: LoadedBundle,
    cycle: datetime,
    plan: ForcingSubset,
    grib_dir: Path,
    through_hour: int | None,
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
    fetched = False
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
                # NOAA requests a ten-second wait between automated filter calls.
                # https://nomads.ncep.noaa.gov/info.php?page=gribfilter
                if fetched:
                    sleep(10)
                response = client.get(
                    _FILTER_URL, params=_filter_params(plan["bbox_wsen_deg"], object_path)
                )
                response.raise_for_status()
                incoming.write_bytes(response.content)
                GFS.validate_file(incoming, cycle, bundle.primary_source, bundle.domain, hour)
                digest = put_idempotent(destination, name, response.content)
                fetched = True
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
    args = parser.parse_args(argv)
    bundle, cycle, plan = load_forcing_plan(args.plan, args.config_dir, GFS)
    with httpx.Client(timeout=120) as client:
        files = _acquire(client, bundle, cycle, plan, args.grib_dir, args.through_hour)
    print(json.dumps(files))
    return 0


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
