"""Exercise normalized ICON GRIB conversion and WPS admission."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from av_wrf.codecs import (
    IconGribCutout,
    WpsIntermediateField,
    read_wps_intermediate_fields,
    write_wps_intermediate_field,
)
from av_wrf.runner.forcing_icon import (
    _SOIL_LAYERS,
    _SOIL_TEMP_SELECTORS,
    ICON_EU,
    ICON_INPUT_SELECTORS,
    ICON_INVARIANT_FIELDS,
    icon_input_metadata,
)
from av_wrf.runner.icon_acquire import _assemble
from av_wrf.runner.icon_intermediate import (
    _expected,
    _inventory_rows,
    _vtable_rows,
    validate_icon_intermediate,
    write_icon_intermediate,
)
from av_wrf.runner.store import LocalFileStore, sha256_hex
from tests.integration import bundle_for_recipe

ec = pytest.importorskip("eccodes")
CONFIG = Path(__file__).resolve().parents[2] / "config"
CYCLE = datetime(2026, 11, 11, 18, tzinfo=UTC)
assert ICON_EU.vtable_text is not None
VTABLE = ICON_EU.vtable_text


def _message(
    codes: tuple[int, int, int, int], level: int, values: list[float], bottom: int | None = None
) -> bytes:
    gid = ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        if codes[3] == 150:
            ec.codes_set(gid, "NV", 6)
        for key, value in {
            "Ni": 2,
            "Nj": 2,
            "latitudeOfFirstGridPointInDegrees": 40,
            "latitudeOfLastGridPointInDegrees": 40.0625,
            "longitudeOfFirstGridPointInDegrees": 44,
            "longitudeOfLastGridPointInDegrees": 44.0625,
            "iDirectionIncrementInDegrees": 0.0625,
            "jDirectionIncrementInDegrees": 0.0625,
            "jScansPositively": 1,
            "discipline": codes[0],
            "parameterCategory": codes[1],
            "parameterNumber": codes[2],
            "typeOfFirstFixedSurface": codes[3],
            "scaleFactorOfFirstFixedSurface": 2 if codes[3] == 106 else 0,
            "scaledValueOfFirstFixedSurface": level,
        }.items():
            ec.codes_set(gid, key, value)
        if bottom is not None:
            ec.codes_set(gid, "typeOfSecondFixedSurface", 106)
            ec.codes_set(gid, "scaleFactorOfSecondFixedSurface", 2)
            ec.codes_set(gid, "scaledValueOfSecondFixedSurface", bottom)
        ec.codes_set_values(gid, np.asarray(values, dtype=np.float64))
        return bytes(ec.codes_get_message(gid))
    finally:
        ec.codes_release(gid)


def test_icon_grib_to_wps_golden() -> None:
    assert _inventory_rows() == _vtable_rows(VTABLE)
    messages = (
        _message((0, 3, 0, 150), 42, [10000, 20000, 30000, 40000]),
        _message((0, 3, 6, 150), 42, [4, 3, 2, 1]),
        _message((0, 1, 0, 150), 42, [0.001, 0.002, 0.003, 0.004]),
        _message((0, 2, 2, 103), 10, [1, 2, 3, 4]),
        _message((0, 3, 1, 101), 0, [101000] * 4),
        _message((2, 3, 18, 106), 5, [270, 271, 272, 273]),
        _message((2, 3, 20, 106), 1, [2, 4, 6, 8], 3),
    )
    out = BytesIO()
    write_icon_intermediate(BytesIO(b"".join(messages)), out, CYCLE, 7, VTABLE)
    out.seek(0)
    slabs = list(read_wps_intermediate_fields(out))
    assert [(s.field.name, s.field.level, s.field.units) for s in slabs] == [
        ("PRESSURE", 42, "Pa"),
        ("HGT", 42, "m"),
        ("SPECHUMD", 42, "kg kg-1"),
        ("UU", 200100, "m s-1"),
        ("PMSL", 201300, "Pa"),
        ("SOILT001", 200100, "K"),
        ("SOILM002", 200100, "m3 m-3"),
    ]
    assert slabs[0].field.valid_time == "2026-11-12_01:00:00"
    assert slabs[0].field.forecast_hours == 7
    assert slabs[0].field.wind_grid_relative is False
    assert (slabs[0].field.south, slabs[0].field.west) == (40, 44)
    np.testing.assert_allclose(slabs[0].values, [[10000, 20000], [30000, 40000]])
    np.testing.assert_allclose(slabs[2].values, [[0.001, 0.002], [0.003, 0.004]], rtol=1e-5)
    np.testing.assert_allclose(slabs[-1].values, [[0.1, 0.2], [0.3, 0.4]])


def _native_message(key: str) -> bytes:
    hour, selector, level = icon_input_metadata(key)
    name = key.split("/")[3]
    gid = ec.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        if selector[3] == 150:
            ec.codes_set(gid, "NV", 6)
        for code, value in {
            "gridType": "unstructured_grid",
            "uuidOfHGrid": "00112233445566778899aabbccddeeff",
            "centre": 78,
            "generatingProcessIdentifier": 2,
            "dataDate": 20261111,
            "dataTime": 1800,
            "indicatorOfUnitOfTimeRange": 0,
            "forecastTime": hour * 60,
            "discipline": selector[0],
            "parameterCategory": selector[1],
            "parameterNumber": selector[2],
            "typeOfFirstFixedSurface": selector[3],
            "scaleFactorOfFirstFixedSurface": 0,
            "scaledValueOfFirstFixedSurface": int(level),
        }.items():
            ec.codes_set(gid, code, value)
        if name == "T_G":
            ec.codes_set(gid, "indicatorOfUnitOfTimeRange", 1)
            ec.codes_set(gid, "forecastTime", hour)
        if name == "T_SO":
            ec.codes_set(gid, "scaleFactorOfFirstFixedSurface", 3 if level == 0.005 else 2)
            ec.codes_set(gid, "scaledValueOfFirstFixedSurface", _SOIL_TEMP_SELECTORS[level])
        if name == "W_SO":
            top = round(level * 100)
            bottom = dict(_SOIL_LAYERS)[top]
            for code, value in {
                "scaleFactorOfFirstFixedSurface": 2,
                "scaledValueOfFirstFixedSurface": top,
                "typeOfSecondFixedSurface": 106,
                "scaleFactorOfSecondFixedSurface": 2,
                "scaledValueOfSecondFixedSurface": bottom,
            }.items():
                ec.codes_set(gid, code, value)
        elif name in {"P", "T", "QV", "U", "V"}:
            ec.codes_set(gid, "typeOfSecondFixedSurface", 150)
            ec.codes_set(gid, "scaleFactorOfSecondFixedSurface", 0)
            ec.codes_set(gid, "scaledValueOfSecondFixedSurface", int(level) + 1)
        values = {
            "CLAT": [40, 40, 40.125, 40.125],
            "CLON": [44, 44.125, 44, 44.125],
            "FR_LAND": [1, 0, 1, 0],
            "HSURF": [100] * 4,
            "HHL": [100 + (75 - level) * 200] * 4,
            "P": [1000 + level * 1200] * 4,
            "H_ICE": [0] * 4,
            "T_G": [275, 285, 275, 285],
        }.get(name, [0.002] * 4 if name.startswith("QV") else [270] * 4)
        if name == "W_SO":
            thickness = (dict(_SOIL_LAYERS)[round(level * 100)] - level * 100) / 100
            values = [300 * thickness, 10 * thickness, 300 * thickness, 10 * thickness]
        ec.codes_set_values(gid, np.asarray(values, dtype=np.float64))
        return bytes(ec.codes_get_message(gid))
    finally:
        ec.codes_release(gid)


def test_assemble_realistic_native_grib_and_inventory(tmp_path: Path) -> None:
    bundle = bundle_for_recipe(CONFIG, ICON_EU)
    keys = ICON_EU.file_keys(CYCLE, (7,))["7"]
    assert not isinstance(keys, str)
    raw = {key: _native_message(key) for key in keys}
    out = tmp_path / "out"
    with (
        patch(
            "av_wrf.runner.icon_acquire._download",
            side_effect=lambda _cycle, key, **_kwargs: (key, sha256_hex(raw[key])),
        ),
        patch.object(LocalFileStore, "get_bytes", lambda _self, key: raw.get(key)),
        patch("av_wrf.codecs.icon_grib._encode", side_effect=AssertionError("unexpected encoding")),
    ):
        files = _assemble(
            bundle, CYCLE, (44, 40, 44.125, 40.125), {7: list(keys)}, tmp_path / "cache", out, 1
        )
    path = out / files[7]
    with patch(
        "av_wrf.runner.icon_intermediate.computational_latlon_bounds",
        return_value=(44, 40, 44.125, 40.125),
    ):
        validate_icon_intermediate(path, CYCLE, bundle.primary_source, bundle.domain, 7, VTABLE)
    slabs = {(s.field.name, s.field.level): s for s in read_wps_intermediate_fields(path)}
    assert len(slabs) == len(_expected(VTABLE)) == 472
    assert {level for name, level in slabs if name == "PRESSURE"} == set(range(1, 75))
    land = slabs["LANDSEA", 200100].values
    np.testing.assert_allclose(land[:, (0, 2)], [[1, 0]] * 3)
    np.testing.assert_allclose(slabs["SKINTEMP", 200100].values, np.where(land, 275, 285))
    for suffix in ("001", "002", "006", "018", "054", "162", "486", "999"):
        soil = slabs[f"SOILM{suffix}", 200100]
        np.testing.assert_allclose(soil.values, [[0.3, 0.3, 0.01]] * 3, rtol=1e-5)
        assert soil.field.units == "m3 m-3"
    inventory = json.loads((out / f"icon-eu.{CYCLE:%Y%m%d%H}.sources-f007.json").read_text())
    assert inventory["source_sha256s"] == {key: sha256_hex(value) for key, value in raw.items()}
    assert inventory["normalized_sha256s"] == {path.name: sha256_hex(path.read_bytes())}

    with (
        patch(
            "av_wrf.runner.icon_acquire._download",
            side_effect=lambda _cycle, key, **_kwargs: (key, sha256_hex(raw[key])),
        ),
        patch.object(LocalFileStore, "get_bytes", lambda _self, key: raw.get(key)),
    ):
        historical = _assemble(
            bundle,
            CYCLE,
            (44, 40, 44.125, 40.125),
            {7: list(keys)},
            tmp_path / "cache",
            tmp_path / "grib",
            1,
            grib=True,
        )
    old = BytesIO()
    with (tmp_path / "grib" / historical[7]).open("rb") as source:
        while gid := ec.codes_grib_new_from_file(source):
            try:
                assert ec.codes_get_long(gid, "validityDate") == 20261112
                assert ec.codes_get_long(gid, "validityTime") == 100
            finally:
                ec.codes_release(gid)
        source.seek(0)
        write_icon_intermediate(source, old, CYCLE, 7, VTABLE)
    old.seek(0)
    for direct, packed in zip(
        read_wps_intermediate_fields(path), read_wps_intermediate_fields(old), strict=True
    ):
        assert direct.field == packed.field
        np.testing.assert_allclose(direct.values, packed.values, rtol=1e-5, atol=1e-5)


def _inventory(
    path: Path, *, missing: bool = False, bad_units: bool = False, mixed_grid: bool = False
) -> None:
    field = WpsIntermediateField(
        "2026-11-12_01:00:00",
        7,
        "DWD",
        "TT",
        "K",
        "test",
        1,
        40,
        44,
        0.0625,
        0.0625,
        6371.229,
        False,
    )
    with path.open("wb") as out:
        for i, ((name, level), units) in enumerate(_expected(VTABLE).items()):
            if missing and i == 0:
                continue
            slab = replace(
                field,
                name=name,
                level=level,
                units="bad" if bad_units and i == 0 else units,
                west=45 if mixed_grid and i == 1 else 44,
            )
            write_wps_intermediate_field(out, slab, np.ones((2, 2)))


def test_icon_direct_validator_rejects_incomplete_or_changed_metadata(tmp_path: Path) -> None:
    bundle = bundle_for_recipe(CONFIG, ICON_EU)
    path = tmp_path / "FILE:2026-11-12_01"
    with patch(
        "av_wrf.runner.icon_intermediate.computational_latlon_bounds",
        return_value=(44, 40, 44.0625, 40.0625),
    ):
        _inventory(path)
        validate_icon_intermediate(path, CYCLE, bundle.primary_source, bundle.domain, 7, VTABLE)
        for flags in ({"missing": True}, {"bad_units": True}, {"mixed_grid": True}):
            _inventory(path, **flags)
            with pytest.raises(ValueError):
                validate_icon_intermediate(
                    path, CYCLE, bundle.primary_source, bundle.domain, 7, VTABLE
                )
        _inventory(path)
        with pytest.raises(ValueError, match="timestamp"):
            validate_icon_intermediate(path, CYCLE, bundle.primary_source, bundle.domain, 6, VTABLE)


def test_invariant_slabs_survive_cache_removal_and_remain_unchanged() -> None:
    keys = ICON_EU.file_keys(CYCLE, (7,))["7"]
    raw = {key: _native_message(key) for key in keys}
    fixed: dict[str, list[str]] = {}
    hourly: dict[str, list[str]] = {}
    for key in keys:
        name = key.split("/")[3]
        group = fixed if name in ICON_INVARIANT_FIELDS else hourly
        group.setdefault(name, []).append(key)
    with patch.object(ec, "codes_get_values", wraps=ec.codes_get_values) as decode:
        cutout = IconGribCutout.from_native(
            fixed,
            raw.__getitem__,
            CYCLE,
            (44, 40, 44.125, 40.125),
            0.0625,
            ICON_INPUT_SELECTORS,
        )
        assert decode.call_count == 79  # Coordinates, land, terrain and 75 height interfaces.
        for names in fixed.values():
            for key in names:
                del raw[key]
        first = list(cutout.fields(hourly, 7, 5000))
        second = list(cutout.fields(hourly, 7, 5000))
        assert decode.call_count == 79 + 2 * len(raw)
    for old, new in zip(first, second, strict=True):
        np.testing.assert_array_equal(old.values, new.values)
    with pytest.raises(ValueError, match="read-only"):
        first[2].values[0, 0] = -1
    one, two = BytesIO(), BytesIO()
    cutout.write_hour(one, hourly, 7, 5000)
    cutout.write_hour(two, hourly, 7, 5000)
    assert one.getvalue() == two.getvalue()
