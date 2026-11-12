"""Exercise NOAA subset requests, validation before install, and retry."""

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from unittest.mock import patch

import httpx
import pytest

from av_wrf.core import resolve_cycle_window
from av_wrf.runner.forcing_gfs import GFS
from av_wrf.runner.gfs_acquire import _acquire, _cgi_fetch, _FetchMode
from av_wrf.runner.mirrors import forcing_subset
from av_wrf.runner.store import sha256_hex
from tests.integration import bundle_for_recipe

CONFIG = Path(__file__).resolve().parents[2] / "config"


def test_cgi_paces_every_attempt_without_retrying_permanent_failure() -> None:
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(404)

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        patch("av_wrf.runner.gfs_acquire.sleep") as pacing,
        patch("av_wrf.runner.http_request.sleep") as retry_sleep,
        pytest.raises(httpx.HTTPStatusError),
    ):
        _cgi_fetch(client, [0, 0, 1, 1], PurePosixPath("dir/file"))
    assert len(calls) == 1
    pacing.assert_called_once_with(10)
    retry_sleep.assert_not_called()


@pytest.mark.parametrize("mode", ["regional", "fallback"])
def test_gfs_subset_retry_reuses_only_verified_files(tmp_path: Path, mode: _FetchMode) -> None:
    bundle = bundle_for_recipe(CONFIG, GFS)
    product = bundle.product
    window = resolve_cycle_window(
        product, bundle.output, f"20261112/{product.publish_origin_hour_utc:02d}"
    )
    _, cycle, plan = forcing_subset(
        product.product_id, window, bundle.primary_source, bundle.domain
    )
    first, second = plan["forecast_hours"][:2]
    first_key, second_key = (plan["file_keys"][str(hour)] for hour in (first, second))
    assert isinstance(first_key, str) and isinstance(second_key, str)
    calls: list[str] = []
    seen_bad_response = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal seen_bad_response
        params = request.url.params
        file = params["file"]
        key = f"{params['dir'].lstrip('/')}/{file}"
        assert request.url.host == "nomads.ncep.noaa.gov"
        assert params["all_var"] == params["all_lev"] == "on" and "subregion" in params
        assert [float(params[k]) for k in ("leftlon", "bottomlat", "rightlon", "toplat")] == (
            plan["bbox_wsen_deg"]
        )
        calls.append(key)
        if key == second_key and not seen_bad_response:
            seen_bad_response = True
            return httpx.Response(200, content=b"not GRIB")
        return httpx.Response(200, content=file.encode())

    def validate(path: Path, date: datetime, *_args: object) -> None:
        assert date == cycle
        if path.read_bytes() == b"not GRIB":
            raise ValueError("invalid forcing")

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        patch(
            "av_wrf.runner.gfs_acquire.fetch_ranges", side_effect=ValueError("mirrors unavailable")
        ) as ranges,
        patch("av_wrf.runner.gfs_acquire.GFS", replace(GFS, validate_file=validate)),
        patch("av_wrf.runner.gfs_acquire.sleep") as wait,
    ):
        with pytest.raises(ValueError, match="invalid forcing"):
            _acquire(client, bundle, cycle, plan, tmp_path, second, mode=mode)
        assert not list(tmp_path.glob("*.json"))
        files = _acquire(client, bundle, cycle, plan, tmp_path, second, mode=mode)
        assert wait.call_count == 3
        if mode == "regional":
            ranges.assert_not_called()
    assert calls == [first_key, second_key, second_key]
    manifest = json.loads(next(tmp_path.glob("*.sources-*.json")).read_bytes())
    assert manifest["normalized_sha256s"] == {
        name: sha256_hex(name.encode()) for name in files.values()
    }
    assert set(manifest["source_sha256s"]) == {first_key, second_key}
