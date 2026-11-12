"""Fetch complete GFS messages selected by the NCEP byte-offset inventory."""

from __future__ import annotations

import logging
import re

import httpx

from av_wrf.runner.http_request import _request

_LOG = logging.getLogger(__name__)
_AIR = frozenset(("TMP", "RH", "UGRD", "VGRD", "HGT"))
_SURFACE = frozenset(("PRES", "HGT", "TMP", "WEASD", "SNOD", "LAND", "LANDN", "ICEC"))
_SOIL = frozenset(
    (
        "0-0.1 m below ground",
        "0.1-0.4 m below ground",
        "0.4-1 m below ground",
        "1-2 m below ground",
        "0.1-2 m below ground",
    )
)


def _selected(variable: str, level: str) -> bool:
    # WPS v4.7 Vtable.GFS and ungrib's default pmin of 100 Pa.
    # https://github.com/wrf-model/WPS/blob/v4.7.0/ungrib/Variable_Tables/Vtable.GFS
    pressure = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?) mb", level)
    if pressure:
        return variable in _AIR and float(pressure.group(1)) >= 1
    if level == "surface":
        return variable in _SURFACE
    if level == "mean sea level":
        return variable == "MSLET"
    if level in _SOIL:
        return variable in ("TSOIL", "TMP", "SOILW")
    if level == "2 m above ground":
        return variable in ("TMP", "RH")
    if level == "10 m above ground":
        return variable in ("UGRD", "VGRD")
    return level in ("max wind", "tropopause") and variable in _AIR | {"PRES"}


def _ranges(index: str, size: int) -> list[tuple[int, int]]:
    entries: list[tuple[int, bool]] = []
    for line in index.splitlines():
        parts = line.split(":")
        if len(parts) < 6 or not parts[0].isdigit() or not parts[1].isdigit():
            raise ValueError("Malformed GFS index")
        offset = int(parts[1])
        if offset >= size or (entries and offset <= entries[-1][0]):
            raise ValueError("Non-monotonic or out-of-range GFS index")
        entries.append((offset, _selected(parts[3], parts[4])))
    if not entries or entries[0][0] != 0:
        raise ValueError("Empty or incomplete GFS index")
    ranges: list[tuple[int, int]] = []
    for i, (start, selected) in enumerate(entries):
        if not selected:
            continue
        end = entries[i + 1][0] - 1 if i + 1 < len(entries) else size - 1
        if ranges and ranges[-1][1] + 1 == start:
            ranges[-1] = (ranges[-1][0], end)
        else:
            ranges.append((start, end))
    if not ranges:
        raise ValueError("GFS index contains no selected messages")
    return ranges


def _framing(data: bytes) -> None:
    offset = 0
    while offset < len(data):
        if len(data) - offset < 20 or data[offset : offset + 4] != b"GRIB":
            raise ValueError("Invalid GRIB2 framing")
        length = int.from_bytes(data[offset + 8 : offset + 16], "big")
        if data[offset + 7] != 2 or length < 20 or offset + length > len(data):
            raise ValueError("Invalid or truncated GRIB2 message")
        if data[offset + length - 4 : offset + length] != b"7777":
            raise ValueError("Invalid GRIB2 terminator")
        offset += length


def _receive_ranges(
    client: httpx.Client, url: str, selected: list[tuple[int, int]], size: int, etag: str
) -> tuple[list[bytes], int]:
    # Without a strong validator, one HTTP response is the only safe way to avoid
    # assembling several object revisions. Discard unselected gaps after receipt.
    requests: list[list[tuple[int, int]]] = []
    for start, end in selected:
        # Merge adjacent messages without increasing transfer bytes when the object is pinned.
        if requests and (not etag or requests[-1][-1][1] + 1 == start):
            requests[-1].append((start, end))
        else:
            requests.append([(start, end)])
    parts: list[bytes] = []
    transferred = 0
    for group in requests:
        start, end = group[0][0], group[-1][1]
        headers = {"Range": f"bytes={start}-{end}", "Accept-Encoding": "identity"}
        if etag:
            headers["If-Match"] = etag
        response = _request(client, "GET", url, headers=headers)
        expected = f"bytes {start}-{end}/{size}"
        if response.status_code != 206 or response.headers.get("content-range") != expected:
            raise ValueError("Invalid HTTP range response")
        if etag and response.headers.get("etag", etag) != etag:
            raise ValueError("Source ETag changed during download")
        data = response.content
        if len(data) != end - start + 1:
            raise ValueError("Truncated HTTP range response")
        transferred += len(data)
        if len(group) == 1:
            parts.append(data)
        else:
            parts.extend(data[a - start : b - start + 1] for a, b in group)
    return parts, transferred


def _source_info(client: httpx.Client, url: str) -> tuple[int, str]:
    head = _request(client, "HEAD", url, headers={"Accept-Encoding": "identity"})
    try:
        size = int(head.headers["content-length"])
    except (KeyError, ValueError) as exc:
        raise ValueError("Missing source content length") from exc
    etag = head.headers.get("etag", "")
    if etag.startswith("W/") or not etag.startswith('"') or not etag.endswith('"'):
        etag = ""
    return size, etag


def _check_source(client: httpx.Client, url: str, size: int, etag: str) -> None:
    if etag:
        final = _request(client, "HEAD", url, headers={"Accept-Encoding": "identity"})
        if final.headers.get("etag") != etag or final.headers.get("content-length") != str(size):
            raise ValueError("Source changed during download")


def _download(client: httpx.Client, url: str) -> bytes:
    size, etag = _source_info(client, url)
    index = _request(client, "GET", url + ".idx").text
    selected = _ranges(index, size)
    parts, transferred = _receive_ranges(client, url, selected, size, etag)
    if _request(client, "GET", url + ".idx").text != index:
        raise ValueError("GFS index changed during download")
    _check_source(client, url, size, etag)
    result = b"".join(parts)
    _framing(result)
    _LOG.info(
        "GFS global messages: transferred=%d selected=%d source=%d", transferred, len(result), size
    )
    return result


def fetch_ranges(client: httpx.Client, mirrors: tuple[str, ...], key: str) -> bytes:
    """Return selected global GRIB2 messages from one complete mirror revision."""
    errors: list[str] = []
    for mirror in mirrors:
        try:
            return _download(client, f"{mirror.rstrip('/')}/{key}")
        except (httpx.HTTPError, ValueError) as exc:
            errors.append(f"{mirror}: {exc}")
            _LOG.warning("GFS mirror failed: %s", errors[-1])
    raise ValueError("All GFS mirrors failed: " + "; ".join(errors))


__all__ = ["fetch_ranges"]
