"""Exercise IFS indexed acquisition and WPS normalization with local fixtures."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import httpx
import numpy as np
import pytest

from av_wrf.codecs import read_wps_intermediate_fields
from av_wrf.core import computational_latlon_bounds, forcing_domain
from av_wrf.runner.bundle import validate_bundle
from av_wrf.runner.forcing_gfs import GFS
from av_wrf.runner.forcing_ifs import AIR, IFS, LEVELS, REQUIRED, SURFACE
from av_wrf.runner.ifs_acquire import _download, _fetch, _index, _normalize, _wanted, _write_hour
from tests.integration import bundle_for_recipe

CONFIG = Path(__file__).resolve().parents[2] / "config"
CYCLE = datetime(2026, 11, 12, 6, tzinfo=UTC)
FRAME = b"GRIB\x00\x00\x00\x02" + (20).to_bytes(8, "big") + b"7777"


@pytest.mark.parametrize(("etag", "gap"), [(True, True), (True, False), (False, True)])
@pytest.mark.parametrize("changed", [True, False])
def test_ifs_index_coherence_and_range_revision(etag: bool, gap: bool, changed: bool) -> None:
    entries = (("z", 0), ("skip", 20), ("sd", 40)) if gap else (("z", 0), ("sd", 20))
    source = FRAME * len(entries)
    index = "\n".join(
        json.dumps({"param": param, "_offset": offset, "_length": 20}) for param, offset in entries
    )
    calls: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        headers = {"etag": '"v1"'} if etag else {}
        if req.method == "HEAD":
            return httpx.Response(200, headers={**headers, "content-length": str(len(source))})
        if req.url.path.endswith(".index"):
            count = sum(r.url.path.endswith(".index") for r in calls)
            return httpx.Response(200, text=index + ("\n" if changed and count == 2 else ""))
        start, end = (int(value) for value in req.headers["range"][6:].split("-"))
        return httpx.Response(
            206,
            headers={**headers, "content-range": f"bytes {start}-{end}/{len(source)}"},
            content=source[start : end + 1],
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        if changed:
            with pytest.raises(ValueError, match="index changed"):
                _download(client, "https://example.invalid/a.grib2", {("z", 0), ("sd", 0)})
        else:
            assert _download(client, "https://example.invalid/a.grib2", {("z", 0), ("sd", 0)}) == {
                ("z", 0): FRAME,
                ("sd", 0): FRAME,
            }
    ranges = [r for r in calls if "range" in r.headers]
    assert len(ranges) == (2 if etag and gap else 1)
    assert all(r.headers.get("if-match") == ('"v1"' if etag else None) for r in ranges)


class TestIfsForcing(TestCase):
    def test_plans_split_objects_and_step_zero_terrain(self) -> None:
        groups = IFS.file_keys(CYCLE, (0, 3))
        self.assertEqual(len(groups["0"]), 2)
        self.assertEqual(len(groups["3"]), 3)
        self.assertEqual(groups["0"][1], groups["3"][2])
        self.assertTrue(groups["3"][0].endswith("20261112060000-3h-oper-fc-pl.grib2"))
        self.assertEqual(len(_wanted("pl")), 70)
        self.assertEqual(len(REQUIRED), 89)

    def test_index_rejects_missing_duplicate_and_invalid_ranges(self) -> None:
        row = {"param": "z", "_offset": 0, "_length": 20}
        text = json.dumps(row)
        self.assertEqual(_index(text, {("z", 0)}, 20)[0].length, 20)
        for bad, wanted, size in (
            (text, {("sd", 0)}, 20),
            (text + "\n" + json.dumps({**row, "_offset": 20}), {("z", 0)}, 40),
            (json.dumps({**row, "_offset": 1}), {("z", 0)}, 21),
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                _index(bad, wanted, size)

    def test_ranges_require_etag_and_retry_mirrors(self) -> None:
        index = json.dumps({"param": "z", "_offset": 0, "_length": 20})
        requests: list[httpx.Request] = []

        def handler(req: httpx.Request) -> httpx.Response:
            requests.append(req)
            if req.url.host == "bad.invalid":
                return httpx.Response(404)
            if req.method == "HEAD":
                return httpx.Response(200, headers={"content-length": "20", "etag": '"abc"'})
            if req.url.path.endswith(".index"):
                return httpx.Response(200, text=index)
            self.assertEqual(req.headers["if-match"], '"abc"')
            return httpx.Response(206, headers={"content-range": "bytes 0-19/20"}, content=FRAME)

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = _fetch(
                client,
                ("https://bad.invalid", "https://good.invalid"),
                "forecast.grib2",
                {("z", 0)},
            )
        self.assertEqual(result, {("z", 0): FRAME})
        self.assertTrue(any(req.url.path.endswith(".index") for req in requests))

        def no_etag(req: httpx.Request) -> httpx.Response:
            if req.method == "HEAD":
                return httpx.Response(200, headers={"content-length": "20"})
            if req.url.path.endswith(".index"):
                return httpx.Response(200, text=index)
            self.assertNotIn("if-match", req.headers)
            return httpx.Response(206, headers={"content-range": "bytes 0-19/20"}, content=FRAME)

        with httpx.Client(transport=httpx.MockTransport(no_etag)) as client:
            self.assertEqual(
                _download(client, "https://good.invalid/a.grib2", {("z", 0)}), {("z", 0): FRAME}
            )

    def test_normalizes_real_grib_fixture_and_water_equivalent(self) -> None:
        ec = pytest.importorskip("eccodes")
        for param, code, value, expected in (
            ("z", 129, 98.0665, 10.0),
            ("sd", 141, 0.1, 100.0),
            ("lsm", 172, 0.5, 0.0),
            ("lsm", 172, 0.6, 1.0),
            ("2t", 167, 290.0, 290.0),
            ("10u", 165, 7.5, 7.5),
        ):
            gid = ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
            try:
                for key, val in {
                    "centre": "ecmf",
                    "paramId": code,
                    "Ni": 4,
                    "Nj": 3,
                    "latitudeOfFirstGridPointInDegrees": 90,
                    "latitudeOfLastGridPointInDegrees": -90,
                    "longitudeOfFirstGridPointInDegrees": 0,
                    "longitudeOfLastGridPointInDegrees": 270,
                    "iDirectionIncrementInDegrees": 90,
                    "jDirectionIncrementInDegrees": 90,
                    "dataDate": 20261112,
                    "dataTime": 600,
                }.items():
                    ec.codes_set(gid, key, val)
                ec.codes_set_values(gid, np.full(12, value))
                raw = ec.codes_get_message(gid)
            finally:
                ec.codes_release(gid)
            name, _, _, values = _normalize(raw, (param, 0), CYCLE, 0, (0, -90, 90, 0), 90)
            self.assertEqual(name, SURFACE[param][0])
            np.testing.assert_allclose(values, expected, rtol=1e-5)

    def test_complete_wps_inventory_and_missing_field_fail_closed(self) -> None:
        fields = [((param, level), b"", 3) for param in AIR for level in LEVELS]
        fields += [((param, 0), b"", 0 if param == "z" else 3) for param in SURFACE]
        fields += [((param, level), b"", 3) for param in ("sot", "vsw") for level in range(1, 5)]

        def normalize(
            raw: bytes, key: tuple[str, int], *_args: object
        ) -> tuple[str, str, float, np.ndarray[tuple[int, int], np.dtype[np.float64]]]:
            del raw
            param, level = key
            if param in AIR:
                name, units = AIR[param]
                output_level = float(level * 100)
            elif param in SURFACE:
                name, units = SURFACE[param]
                output_level = 201300.0 if param == "msl" else 200100.0
            else:
                suffix = ("000007", "007028", "028100", "100289")[level - 1]
                name, units, output_level = (
                    ("ST" if param == "sot" else "SM") + suffix,
                    ("K" if param == "sot" else "m3 m-3"),
                    200100.0,
                )
            value = {"2d": 273.16, "sp": 100_000, "sd": 50.0, "rsn": 250.0}.get(param, 1.0)
            return name, units, output_level, np.full((2, 2), value, dtype=np.float64)

        with patch("av_wrf.runner.ifs_acquire._normalize", normalize):
            data = _write_hour(CYCLE, 3, (0, 0, 1, 1), 1, 6370000, fields)
            with self.assertRaisesRegex(ValueError, "Missing IFS fields"):
                _write_hour(CYCLE, 3, (0, 0, 1, 1), 1, 6370000, fields[:-1])
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.wps"
            path.write_bytes(data)
            slabs = list(read_wps_intermediate_fields(path, required_fields=REQUIRED))
        self.assertEqual(len(slabs), len(REQUIRED))
        self.assertEqual({s.field.valid_time for s in slabs}, {"2026-11-12_09:00:00"})
        near_surface = {s.field.name: s.values for s in slabs if s.field.level == 200100}
        np.testing.assert_allclose(near_surface["SPECHUMD"], 0.00381053, rtol=1e-6)
        np.testing.assert_allclose(near_surface["SNOWH"], 0.2)
        np.testing.assert_allclose(near_surface["SNOW"], 50.0)

    def test_recipe_requires_specific_humidity(self) -> None:
        bundle = bundle_for_recipe(CONFIG, GFS)
        IFS.check_window(bundle.candidate_source, bundle.domain, CYCLE, (0, 3))
        with self.assertRaisesRegex(ValueError, "three-hourly"):
            IFS.check_window(bundle.candidate_source, bundle.domain, CYCLE, (0, 1))
        for source in (
            replace(bundle.candidate_source, use_sh_qv=False),
            replace(bundle.candidate_source, metgrid_levels=16),
            replace(bundle.candidate_source, metgrid_soil_levels=5),
        ):
            with (
                self.subTest(source=source),
                self.assertRaisesRegex(
                    ValueError,
                    "IFS requires specific humidity, 15 metgrid levels and 4 soil layers",
                ),
            ):
                validate_bundle(
                    replace(bundle, primary_source=source), publish_origin_utc="20261112/00"
                )


@pytest.mark.parametrize(
    ("expected_west", "written_west", "valid"),
    [(37.9, 37.9, True), (36.3, 36.3, True), (179.9, -180.1, True), (37.9, 38.0, False)],
)
def test_ifs_coverage_wraps_serialized_west_edge(
    tmp_path: Path, expected_west: float, written_west: float, valid: bool
) -> None:
    from av_wrf.codecs import WpsIntermediateField, write_wps_intermediate_field
    from av_wrf.runner.forcing_ifs import UNITS

    bundle = bundle_for_recipe(CONFIG, GFS)
    source = bundle.candidate_source
    spacing = source.grid_spacing_deg
    domain = forcing_domain(bundle.domain, spacing)
    _, south, _, north = computational_latlon_bounds(domain, snap_deg=spacing)
    data = np.ones((round((north - south) / spacing) + 1, 3))
    path = tmp_path / "forcing.wps"
    with path.open("wb") as stream:
        for name, level in sorted(REQUIRED):
            field = WpsIntermediateField(
                "2026-11-12_06:00:00",
                0.0,
                "ECMWF IFS",
                name,
                UNITS[name],
                name,
                level,
                south,
                written_west,
                spacing,
                spacing,
                domain.earth_radius_m / 1000,
                False,
            )
            write_wps_intermediate_field(stream, field, data)
    bounds = (expected_west, south, expected_west + 2 * spacing, north)
    with patch("av_wrf.runner.forcing_ifs.computational_latlon_bounds", return_value=bounds):
        if valid:
            IFS.validate_file(path, CYCLE, source, bundle.domain, 0)
        else:
            with pytest.raises(ValueError, match="coverage"):
                IFS.validate_file(path, CYCLE, source, bundle.domain, 0)
