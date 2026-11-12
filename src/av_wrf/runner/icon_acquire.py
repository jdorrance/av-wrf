"""Fetch and normalize native ICON-EU inputs for an existing av-wrf prep plan."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import partial
from io import BytesIO
from pathlib import Path
from time import sleep

import httpx

from av_wrf.codecs import IconGribCutout
from av_wrf.core import resolve_cycle_window
from av_wrf.runner.bundle import LoadedBundle, load_bundle
from av_wrf.runner.forcing_icon import ICON_EU, ICON_INPUT_SELECTORS, ICON_INVARIANT_FIELDS
from av_wrf.runner.mirrors import _forcing_subset
from av_wrf.runner.store import LocalFileStore, json_bytes, put_idempotent, sha256_hex

_WORKERS = 24
type _Bounds = tuple[float, float, float, float]
type _ObjectKeysByHour = dict[int, list[str]]


def _retry_delay(error: httpx.HTTPError, delay: float) -> float | None:
    if not isinstance(error, httpx.HTTPStatusError):
        return delay
    status = error.response.status_code
    if status != 429 and not 500 <= status < 600:
        return None
    header = error.response.headers.get("Retry-After", "").strip()
    if header.isascii() and header.isdecimal():
        return float(header)
    if header:
        try:
            retry_at = parsedate_to_datetime(header)
        except (ValueError, OverflowError):
            return delay
        if retry_at.tzinfo is not None:
            return max(delay, (retry_at - datetime.now(UTC)).total_seconds())
    return delay


def _read_plan(
    plan_path: Path, config_root: Path
) -> tuple[LoadedBundle, datetime, _Bounds, _ObjectKeysByHour]:
    doc = json.loads(plan_path.read_bytes())
    if not isinstance(doc, dict):
        raise ValueError("Expected an ICON-EU forcing_subset.json object")
    product, partition = doc.get("product_id"), doc.get("cycle_partition")
    if not isinstance(product, str) or not isinstance(partition, str):
        raise ValueError("Forcing plan requires product_id and cycle_partition")
    bundle = load_bundle(config_root, product)
    window = resolve_cycle_window(bundle.product, bundle.output, partition)
    recipe, cycle, expected = _forcing_subset(product, window, bundle.primary_source, bundle.domain)
    if recipe is not ICON_EU or any(doc.get(key) != value for key, value in expected.items()):
        raise ValueError("Forcing plan differs from the configured ICON-EU inventory")
    groups: _ObjectKeysByHour = {}
    for hour, keys in expected["file_keys"].items():
        if isinstance(keys, str):
            raise ValueError("Expected an ICON-EU object inventory per boundary hour")
        groups[int(hour)] = keys
    west, south, east, north = expected["bbox_wsen_deg"]
    return bundle, cycle, (west, south, east, north), groups


def _has_grib2_frame(raw: bytes) -> bool:
    # GRIB2 section 0 carries the edition and total message length.
    # https://www.nco.ncep.noaa.gov/pmb/docs/grib2/grib2_doc/grib2_sect0.shtml
    return (
        len(raw) >= 20
        and raw.startswith(b"GRIB")
        and raw[7] == 2
        and int.from_bytes(raw[8:16], "big") == len(raw)
        and raw.endswith(b"7777")
    )


def _download(
    key: str,
    *,
    client: httpx.Client,
    mirrors: tuple[str, ...],
    cache: LocalFileStore,
    root: Path,
) -> None:
    if (root / key).is_file():
        return
    last_error: httpx.HTTPError | ValueError | None = None
    for mirror in mirrors:
        url = f"{mirror.rstrip('/')}/{key}"
        for backoff in (0.25, 0.5, None):
            try:
                response = client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                last_error = exc
                if backoff is None:
                    break
                delay = _retry_delay(exc, backoff)
                if delay is None or delay > 30.0:
                    break
                sleep(delay)
                continue
            raw = response.content
            if _has_grib2_frame(raw):
                put_idempotent(cache, key, raw)
                return
            last_error = ValueError(f"Expected one complete DWD GRIB2 message from {url}")
            break
    raise RuntimeError(f"ICON-EU forcing object unavailable: {key}") from last_error


def _group_keys(keys: Iterable[str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for key in keys:
        grouped[key.split("/", 4)[3]].append(key)
    return grouped


def _assemble(
    bundle: LoadedBundle,
    cycle: datetime,
    bounds: _Bounds,
    keys_by_hour: _ObjectKeysByHour,
    cache_root: Path,
    out: Path,
    workers: int,
) -> dict[int, str]:
    source = bundle.primary_source
    fixed_fields = {
        field: keys
        for field, keys in _group_keys(next(iter(keys_by_hour.values()))).items()
        if field in ICON_INVARIANT_FIELDS
    }
    fixed_set = {key for keys in fixed_fields.values() for key in keys}
    hourly_keys = (key for keys in keys_by_hour.values() for key in keys if key not in fixed_set)
    limits = httpx.Limits(max_connections=workers, max_keepalive_connections=workers)
    cache = LocalFileStore(cache_root)
    raw_shas: dict[str, str] = {}

    def read_object(key: str) -> bytes:
        raw = cache.get_bytes(key)
        if raw is None:
            raise FileNotFoundError(f"Missing cached ICON-EU object: {key}")
        raw_shas[key] = sha256_hex(raw)
        return raw

    with httpx.Client(limits=limits, timeout=30) as client, ThreadPoolExecutor(workers) as pool:
        fetch = partial(
            _download, client=client, mirrors=source.mirrors, cache=cache, root=cache_root
        )
        list(pool.map(fetch, fixed_set))
        cutout = IconGribCutout.from_native(
            fixed_fields,
            read_object,
            cycle,
            bounds,
            source.grid_spacing_deg,
            ICON_INPUT_SELECTORS,
        )
        list(pool.map(fetch, hourly_keys))
    dest = LocalFileStore(out)
    prefix = f"icon-eu.{cycle:%Y%m%d%H}"
    files: dict[int, str] = {}
    normalized_shas: dict[str, str] = {}
    for hour, keys in keys_by_hour.items():
        fields = _group_keys(key for key in keys if key not in fixed_set)
        stream = BytesIO()
        cutout.write_hour(stream, fields, hour, bundle.domain.p_top_pa)
        name = f"{prefix}.f{hour:03d}.grib2"
        normalized_shas[name] = put_idempotent(dest, name, stream.getvalue())
        files[hour] = name
    inventory = {
        "forcing_cycle_utc": cycle.isoformat(),
        "source_sha256s": raw_shas,
        "normalized_sha256s": normalized_shas,
    }
    put_idempotent(dest, f"{prefix}.sources-f{max(keys_by_hour):03d}.json", json_bytes(inventory))
    return files


def main(argv: list[str] | None = None) -> int:
    """Print the normalized hour-to-filename inventory for forcing-manifest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True, help="Path to forcing_subset.json")
    parser.add_argument("--config-dir", type=Path, default=Path("config"))
    parser.add_argument("--cache", type=Path, required=True, help="Retain DWD source objects here")
    parser.add_argument("--grib-dir", type=Path, required=True, help="Write normalized GRIB2 here")
    parser.add_argument("--workers", type=int, default=_WORKERS, help="Concurrent DWD downloads")
    parser.add_argument("--through-hour", type=int, help="Last boundary lead for a smoke run")
    args = parser.parse_args(argv)
    if args.workers < 1:
        parser.error("--workers must be positive")
    bundle, cycle, bounds, keys_by_hour = _read_plan(args.plan, args.config_dir)
    if args.through_hour is not None:
        keys_by_hour = {
            hour: keys for hour, keys in keys_by_hour.items() if hour <= args.through_hour
        }
        if not keys_by_hour:
            parser.error("--through-hour precedes the first required forecast hour")
    files = _assemble(bundle, cycle, bounds, keys_by_hour, args.cache, args.grib_dir, args.workers)
    print(json.dumps(files))
    return 0


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
