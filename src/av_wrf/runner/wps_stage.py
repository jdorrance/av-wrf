"""Stage hash-verified forcing snapshots for native GRIB or direct WPS input."""

from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

from av_wrf.contracts import ForcingManifest, parse_iso_utc


def _alpha_suffix(index: int) -> str:
    if index < 0 or index >= 26**3:
        raise ValueError("Too many GRIB input files for 3-letter ungrib suffix")
    return f"{chr(65 + index // 676)}{chr(65 + (index // 26) % 26)}{chr(65 + index % 26)}"


def stage_forcing_links(attempt_dir: Path, manifest: ForcingManifest) -> None:
    """Link a uniform, already validated source inventory; reject mixed binary formats."""
    cycle = parse_iso_utc(manifest.forcing_cycle_utc)
    signature: bytes | None = None
    links: list[tuple[str, str]] = []
    for index, (hour, (name, _)) in enumerate(
        zip(manifest.forecast_hours, manifest.file_sha256s, strict=True)
    ):
        with (attempt_dir / "inputs" / "grib" / name).open("rb") as stream:
            current = stream.read(4)
        if current not in (b"GRIB", b"\x00\x00\x00\x04"):
            raise ValueError(f"Unsupported forcing binary format: {name}")
        if signature is not None and current != signature:
            raise ValueError("Mixed GRIB and WPS intermediate forcing files")
        signature = current
        # metgrid reads FILE:valid_hour directly; only actual GRIB streams need ungrib.
        target = (
            f"GRIBFILE.{_alpha_suffix(index)}"
            if current == b"GRIB"
            else f"FILE:{cycle + timedelta(hours=hour):%Y-%m-%d_%H}"
        )
        links.append((name, target))
    if not links:
        raise ValueError("Empty forcing inventory")
    for name, target in links:
        os.symlink(f"inputs/grib/{name}", attempt_dir / target)


def native_wps_stages(attempt_dir: Path) -> tuple[str, ...]:
    """Select ungrib only when staged inputs contain actual GRIB messages."""
    return ("ungrib", "metgrid") if any(attempt_dir.glob("GRIBFILE.*")) else ("metgrid",)


__all__ = ["native_wps_stages", "stage_forcing_links"]
