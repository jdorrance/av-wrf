"""Exercise GFS index selection and HTTP revision safety."""

from collections.abc import Callable
from unittest.mock import patch

import httpx
import pytest

from av_wrf.runner.range_fetch import _ranges, _selected, fetch_ranges


def _message(payload: bytes) -> bytes:
    return b"GRIB\x00\x00\x00\x02" + (20 + len(payload)).to_bytes(8, "big") + payload + b"7777"


def _fixture() -> tuple[bytes, str, bytes]:
    chunks = [_message(bytes([n])) for n in range(5)]
    names = [
        ("HGT", "1 mb"),
        ("TMP", "1 mb"),
        ("SPFH", "1 mb"),
        ("RH", "1 mb"),
        ("PRES", "surface"),
    ]
    offset = 0
    lines = []
    for i, (chunk, (var, level)) in enumerate(zip(chunks, names, strict=True), 1):
        lines.append(f"{i}:{offset}:d=2026111200:{var}:{level}:anl:")
        offset += len(chunk)
    return b"".join(chunks), "\n".join(lines), b"".join(chunks[:2] + chunks[3:])


def _server(
    *, etag: bool = True, broken: str = "", failure: Callable[[httpx.Request], bool] | None = None
) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    data, index, _ = _fixture()
    calls: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if failure and failure(request):
            return httpx.Response(503)
        headers = {"etag": '"v1"'} if etag else {}
        if request.method == "HEAD":
            return httpx.Response(200, headers={**headers, "content-length": str(len(data))})
        if request.url.path.endswith(".idx"):
            count = sum(r.url.path.endswith(".idx") for r in calls)
            text = index + ("\n" if broken == "index" and count > 1 else "")
            return httpx.Response(200, text=text)
        if broken == "revision" and sum("range" in r.headers for r in calls) > 1:
            return httpx.Response(412)
        start, end = (int(x) for x in request.headers["range"][6:].split("-"))
        payload = data[start : end + 1]
        if broken == "truncated":
            payload = payload[:-1]
        if broken == "framing":
            payload = b"X" + payload[1:]
        if broken == "bad-range":
            return httpx.Response(200, content=payload)
        headers["content-range"] = f"bytes {start}-{end}/{len(data)}"
        return httpx.Response(206, headers=headers, content=payload)

    return httpx.MockTransport(respond), calls


@pytest.mark.parametrize("etag", [True, False])
def test_deterministic_ranges_and_pin(etag: bool) -> None:
    transport, calls = _server(etag=etag)
    with httpx.Client(transport=transport) as client:
        assert fetch_ranges(client, ("https://a",), "key") == _fixture()[2]
    requests = [r for r in calls if "range" in r.headers]
    assert len(requests) == (2 if etag else 1)
    assert all(r.headers.get("if-match") == ('"v1"' if etag else None) for r in requests)


@pytest.mark.parametrize("broken", ["revision", "truncated", "framing", "bad-range", "index"])
def test_bad_mirror_never_returns_partial_output(broken: str) -> None:
    transport, _ = _server(broken=broken)
    with httpx.Client(transport=transport) as client, pytest.raises(ValueError):
        fetch_ranges(client, ("https://a",), "key")


def test_retry_and_mirror_failover() -> None:
    attempts = 0

    def fail(request: httpx.Request) -> bool:
        nonlocal attempts
        if request.url.host == "a":
            return True
        if request.method == "HEAD" and attempts == 0:
            attempts += 1
            return True
        return False

    transport, calls = _server(failure=fail)
    with httpx.Client(transport=transport) as client, patch("av_wrf.runner.http_request.sleep"):
        assert fetch_ranges(client, ("https://a", "https://b"), "key") == _fixture()[2]
    assert sum(r.url.host == "a" for r in calls) == 3
    assert sum(r.url.host == "b" and r.method == "HEAD" for r in calls) == 3


def test_precondition_failure_is_not_retried() -> None:
    transport, calls = _server(broken="revision")
    with (
        httpx.Client(transport=transport) as client,
        patch("av_wrf.runner.http_request.sleep") as sleep,
        pytest.raises(ValueError, match="All GFS mirrors failed"),
    ):
        fetch_ranges(client, ("https://a",), "key")
    assert len([r for r in calls if "range" in r.headers]) == 2
    sleep.assert_not_called()


@pytest.mark.parametrize(
    ("variable", "level", "expected"),
    [
        ("HGT", "0.7 mb", False),
        ("HGT", "1 mb", True),
        ("SPFH", "1000 mb", False),
        ("MSLET", "mean sea level", True),
        ("PRMSL", "mean sea level", False),
        ("TSOIL", "0-0.1 m below ground", True),
        ("SOILL", "0-0.1 m below ground", False),
        ("TMP", "2 m above ground", True),
        ("RH", "2 m above ground", True),
        ("UGRD", "10 m above ground", True),
        ("HGT", "tropopause", True),
        ("WEASD", "surface", True),
        ("ICEC", "surface", True),
    ],
)
def test_wps_inventory_selection(variable: str, level: str, expected: bool) -> None:
    assert _selected(variable, level) is expected


def test_reject_malformed_index() -> None:
    with pytest.raises(ValueError):
        _ranges("1:1:d=x:TMP:1 mb:anl:", 100)
