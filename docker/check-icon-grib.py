"""Exercise real ecCodes normalization and optionally native WPS ungrib offline."""

import argparse
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

import eccodes as ec
import httpx
import numpy as np

from av_wrf.codecs import IconGribCutout
from av_wrf.runner.forcing_icon import ICON_EU, ICON_INPUT_SELECTORS
from av_wrf.runner.grib import GRIB_KEYS, checked_grib_grid, inspect_grib
from av_wrf.runner.icon_acquire import _download
from av_wrf.runner.store import LocalFileStore

_CYCLE = datetime(2026, 1, 1, 18, tzinfo=UTC)
_HOUR = 7
_SPACING = 0.0625
_BOUNDS = (0.0, 0.0, 2 * _SPACING, 2 * _SPACING)


def _message(
    selector: tuple[int, int, int, int],
    values: tuple[float, ...],
    *,
    level: int = 0,
    hour: int = 0,
) -> bytes:
    gid = ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        if selector[3] == 150:
            ec.codes_set(gid, "NV", 6)  # GRIB2 general vertical height coordinate.
        ec.codes_set_key_vals(
            gid,
            {
                "gridType": "unstructured_grid",
                "uuidOfHGrid": "00112233445566778899aabbccddeeff",
                "centre": 78,
                "generatingProcessIdentifier": 2,
                "dataDate": int(_CYCLE.strftime("%Y%m%d")),
                "dataTime": _CYCLE.hour * 100,
                "indicatorOfUnitOfTimeRange": 0,
                "forecastTime": hour * 60,
                "discipline": selector[0],
                "parameterCategory": selector[1],
                "parameterNumber": selector[2],
                "typeOfFirstFixedSurface": selector[3],
                "scaledValueOfFirstFixedSurface": level,
            },
        )
        if selector == ICON_INPUT_SELECTORS["P"]:
            ec.codes_set(gid, "typeOfSecondFixedSurface", 150)
            ec.codes_set(gid, "scaleFactorOfSecondFixedSurface", 0)
            ec.codes_set(gid, "scaledValueOfSecondFixedSurface", level + 1)
        ec.codes_set_values(gid, values)
        return cast(bytes, ec.codes_get_message(gid))
    finally:
        ec.codes_release(gid)


def _check(path: Path) -> None:
    edge = _BOUNDS[2]
    selectors = ICON_INPUT_SELECTORS
    raw = {
        "lat": _message((0, 191, 1, 1), (0, 0, edge, edge)),
        "lon": _message((0, 191, 2, 1), (0, edge, 0, edge)),
        "land": _message(selectors["FR_LAND"], (1, 0, 1, 0)),
        "terrain": _message(selectors["HSURF"], (100,) * 4),
        "top": _message(selectors["HHL"], (10_000,) * 4, level=1),
        "bottom": _message(selectors["HHL"], (100,) * 4, level=2),
        "pressure": _message(selectors["P"], (3_000,) * 4, level=1, hour=_HOUR),
        "skin": _message(selectors["T_G"], (275, 285, 275, 285), hour=_HOUR),
    }
    _check_admission(path.parent, raw["skin"], raw["pressure"])
    fixed = {
        "CLAT": ("lat",),
        "CLON": ("lon",),
        "FR_LAND": ("land",),
        "HSURF": ("terrain",),
        "HHL": ("top", "bottom"),
    }
    cutout = IconGribCutout.from_native(
        fixed, raw.__getitem__, _CYCLE, _BOUNDS, _SPACING, selectors
    )
    with path.open("w+b") as stream:
        cutout.write_hour(stream, {"P": ("pressure",), "T_G": ("skin",)}, _HOUR, 5_000)
        stream.seek(0)
        values = []
        while (gid := ec.codes_grib_new_from_file(stream)) is not None:
            try:
                keys = ("gridType", "packingType", "numberOfDataPoints", "numberOfValues")
                assert tuple(ec.codes_get(gid, key) for key in keys) == (
                    "regular_ll",
                    "grid_simple",
                    9,
                    9,
                )
                assert ec.codes_get_long(gid, "validityDate") == 20260102
                assert ec.codes_get_long(gid, "validityTime") == 100
                values.append(ec.codes_get_values(gid))
            finally:
                ec.codes_release(gid)
    land, terrain, height, pressure, skin = values
    np.testing.assert_array_equal(land.reshape(3, 3), [[1, 1, 0]] * 3)
    np.testing.assert_allclose(terrain, 100)
    np.testing.assert_allclose(height, 5050)
    np.testing.assert_allclose(pressure, 3000)
    np.testing.assert_allclose(skin, np.where(land == 1, 275, 285))
    print("ICON GRIB2 cutout passed decoding, geometry, valid time, and value checks")


def _check_admission(work: Path, wrong: bytes, correct: bytes) -> None:
    key = next(key for key in ICON_EU.file_keys(_CYCLE, (_HOUR,))[str(_HOUR)] if "/P/" in key)
    cache_root = work / "cache"
    cache = LocalFileStore(cache_root)
    gid = ec.codes_new_from_message(correct)
    try:
        offset = ec.codes_get_offset(gid, "dataRepresentationTemplateNumber")
        ec.codes_set(gid, "generatingProcessIdentifier", 255)
        wrong_process = cast(bytes, ec.codes_get_message(gid))
        ec.codes_set(gid, "generatingProcessIdentifier", 2)
        ec.codes_set(gid, "stepType", "avg")
        assert ec.codes_get_long(gid, "validityDate") == 20260102
        assert ec.codes_get_long(gid, "validityTime") == 100
        statistical = cast(bytes, ec.codes_get_message(gid))
    finally:
        ec.codes_release(gid)
    corrupt = correct[:offset] + b"\xff\xff" + correct[offset + 2 :]
    responses = (wrong, wrong_process, statistical, corrupt, correct)
    bodies = iter(responses)
    transport = httpx.MockTransport(lambda _: httpx.Response(200, content=next(bodies)))
    with httpx.Client(transport=transport) as client:
        _download(
            _CYCLE,
            key,
            client=client,
            mirrors=tuple(f"https://mirror-{index}.invalid" for index, _ in enumerate(responses)),
            cache=cache,
            root=cache_root,
        )
    assert cache.get_bytes(key) == correct


def _check_wps(path: Path, ungrib: Path) -> None:
    rows = inspect_grib(path, GRIB_KEYS)
    assert len(list(checked_grib_grid(path, rows, _BOUNDS, _SPACING, ("grid_simple",)))) == 5
    assert ICON_EU.vtable_text is not None
    work = path.parent
    valid = _CYCLE + timedelta(hours=_HOUR)
    (work / "Vtable").write_text(ICON_EU.vtable_text, encoding="ascii")
    (work / "namelist.wps").write_text(
        f"&share\n wrf_core='ARW', max_dom=1, interval_seconds=3600,\n"
        f" start_date='{valid:%Y-%m-%d_%H:%M:%S}', end_date='{valid:%Y-%m-%d_%H:%M:%S}',\n"
        "/\n&ungrib\n out_format='WPS', prefix='FILE',\n/\n",
        encoding="ascii",
    )
    result = subprocess.run([str(ungrib)], cwd=work, capture_output=True, text=True, timeout=30)
    if result.returncode or not (work / f"FILE:{valid:%Y-%m-%d_%H}").is_file():
        raise RuntimeError(result.stdout + result.stderr)
    print("WPS ungrib produced an ICON intermediate file")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ungrib", type=Path)
    args = parser.parse_args()
    with TemporaryDirectory() as tmp:
        grib = Path(tmp) / "GRIBFILE.AAA"
        _check(grib)
        if args.ungrib is not None:
            _check_wps(grib, args.ungrib)
