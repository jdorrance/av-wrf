"""Check WPS sequential records independently of the field writer."""

import struct
from dataclasses import replace
from io import BytesIO
from pathlib import Path

import numpy as np
import pytest

from av_wrf.codecs import (
    WpsIntermediateField,
    read_wps_intermediate_fields,
    write_wps_intermediate_field,
)


def test_wps_v5_record_layout_and_grid_relative_flag() -> None:
    field = WpsIntermediateField(
        "2026-11-12_03:00:00",
        3,
        "IFS",
        "UU",
        "m s-1",
        "wind",
        85000,
        35,
        40,
        0.1,
        0.1,
        6371.229,
        False,
    )
    values = np.array([[1, 2, 3], [4, 5, 6]], dtype=np.float64)
    stream = BytesIO()
    write_wps_intermediate_field(stream, field, values)
    stream.seek(0)
    records = []
    for _ in range(5):
        size = struct.unpack(">i", stream.read(4))[0]
        records.append(stream.read(size))
        assert struct.unpack(">i", stream.read(4))[0] == size
    assert stream.read() == b""
    assert struct.unpack(">i", records[0]) == (5,)
    header = struct.unpack(">24sf32s9s25s46sfiii", records[1])
    assert header[0].strip() == b"2026-11-12_03:00:00"
    assert header[3].strip() == b"UU"
    assert header[-4:] == (85000, 3, 2, 0)
    assert records[2][:8] == b"SWCORNER"
    assert struct.unpack(">i", records[3]) == (0,)
    assert struct.unpack(">6f", records[4]) == (1, 2, 3, 4, 5, 6)
    stream = BytesIO()
    write_wps_intermediate_field(stream, replace(field, wind_grid_relative=True), values)
    assert stream.getvalue() != b""


def test_wps_rejects_invalid_slab_and_metadata() -> None:
    field = WpsIntermediateField(
        "2026-11-12_03:00:00",
        3,
        "IFS",
        "TT",
        "K",
        "temperature",
        85000,
        35,
        40,
        0.1,
        0.1,
        6371.229,
        False,
    )
    with pytest.raises(ValueError, match="finite"):
        write_wps_intermediate_field(BytesIO(), field, np.full((2, 2), np.nan))
    with pytest.raises(ValueError, match="grid"):
        write_wps_intermediate_field(BytesIO(), replace(field, latitude_step=0), np.ones((2, 2)))


def _field(
    *, name: str = "TT", units: str = "K", valid_time: str = "2026-11-12_03:00:00"
) -> WpsIntermediateField:
    return WpsIntermediateField(
        valid_time,
        3,
        "IFS",
        name,
        units,
        "temperature",
        85000,
        35,
        40,
        0.1,
        0.1,
        6371.229,
        False,
    )


def _encoded(*fields: WpsIntermediateField) -> bytes:
    stream = BytesIO()
    for field in fields:
        write_wps_intermediate_field(stream, field, np.arange(6, dtype=np.float64).reshape(2, 3))
    return stream.getvalue()


def _offsets(data: bytes | bytearray) -> list[int]:
    offsets = []
    offset = 0
    for _ in range(5):
        offsets.append(offset)
        offset += 8 + struct.unpack_from(">i", data, offset)[0]
    return offsets


def test_read_wps_roundtrip_stream_path_and_inventory(tmp_path: Path) -> None:
    data = _encoded(_field(), _field(name="UU", units="m s-1"))
    path = tmp_path / "FILE"
    path.write_bytes(data)
    slabs = list(
        read_wps_intermediate_fields(
            path,
            expected_time="2026-11-12_03:00:00",
            required_fields={("TT", 85000), ("UU", 85000)},
        )
    )
    assert [slab.field.name for slab in slabs] == ["TT", "UU"]
    assert slabs[0].values.dtype == np.float32
    np.testing.assert_array_equal(slabs[0].values, np.arange(6).reshape(2, 3))
    assert slabs[0].field.latitude_step == pytest.approx(0.1)
    stream = BytesIO(data)
    assert len(list(read_wps_intermediate_fields(stream))) == 2
    assert not stream.closed


def test_read_wps_supports_short_stream_reads() -> None:
    class ShortReads(BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            return super().read(min(size, 3) if size is not None and size >= 0 else size)

    assert len(list(read_wps_intermediate_fields(ShortReads(_encoded(_field()))))) == 1


@pytest.mark.parametrize("cut", [0, 1, 3, 4, 11, 12, 20, -1, -4, -10])
def test_read_wps_rejects_truncation(cut: int) -> None:
    data = _encoded(_field())
    with pytest.raises(ValueError, match=r"Truncated|Empty"):
        list(read_wps_intermediate_fields(BytesIO(data[:cut])))


@pytest.mark.parametrize(
    ("record", "offset", "payload", "error"),
    [
        (0, 0, struct.pack(">i", 2**31 - 1), "length"),
        (0, 4, struct.pack(">i", 4), "version"),
        (0, 8, struct.pack(">i", 3), "markers"),
        (1, 4, b"2026-02-30_03:00:00", "timestamp"),
        (1, 4, b"2026-1-12_03:00:00 ", "timestamp"),
        (1, 4 + 24, struct.pack(">f", float("nan")), "metadata"),
        (1, 4 + struct.calcsize(">24sf32s9s25s46sf"), struct.pack(">i", 0), "dimensions"),
        (1, 4 + struct.calcsize(">24sf32s9s25s46sfii"), struct.pack(">i", 1), "projection"),
        (2, 4, b"BADSTART", "origin"),
        (3, 4, struct.pack(">i", 1), "wind flag"),
        (3, 4, struct.pack(">i", -1), "wind flag"),
        (4, 0, struct.pack(">i", 28), "length"),
        (4, 4, struct.pack(">f", float("inf")), "Non-finite"),
    ],
)
def test_read_wps_rejects_corrupt_records(
    record: int, offset: int, payload: bytes, error: str
) -> None:
    data = bytearray(_encoded(_field()))
    start = _offsets(data)[record] + offset
    data[start : start + len(payload)] = payload
    with pytest.raises(ValueError, match=error):
        list(read_wps_intermediate_fields(BytesIO(data)))


def test_read_wps_rejects_huge_grid_before_reading_payload() -> None:
    data = bytearray(_encoded(_field()))
    header = _offsets(data)[1] + 4
    nx = header + struct.calcsize(">24sf32s9s25s46sf")
    struct.pack_into(">ii", data, nx, 100000, 100000)
    with pytest.raises(ValueError, match="dimensions"):
        list(read_wps_intermediate_fields(BytesIO(data)))


def test_read_wps_rejects_missing_duplicate_or_wrong_time() -> None:
    with pytest.raises(ValueError, match="Missing required"):
        list(
            read_wps_intermediate_fields(
                BytesIO(_encoded(_field())), required_fields={("UU", 85000)}
            )
        )
    with pytest.raises(ValueError, match="Duplicate"):
        list(read_wps_intermediate_fields(BytesIO(_encoded(_field(), _field()))))
    with pytest.raises(ValueError, match="timestamp mismatch"):
        list(
            read_wps_intermediate_fields(
                BytesIO(_encoded(_field())), expected_time="2026-11-12_00:00:00"
            )
        )
    with pytest.raises(ValueError, match="timestamp mismatch"):
        list(
            read_wps_intermediate_fields(
                BytesIO(_encoded(_field(), _field(name="UU", valid_time="2026-11-12_06:00:00")))
            )
        )
