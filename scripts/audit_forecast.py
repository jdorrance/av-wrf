"""Verify a local forecast, optionally comparing a baseline and auditing RRTMG shortwave."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections.abc import Iterable
from pathlib import Path

import netCDF4
import numpy as np
import numpy.typing as npt
import zarr
from zarr.types import AnyArray

from av_wrf.codecs import domain_tag, select_cycle_wrfout_slices
from av_wrf.codecs.wrf_nc import RRTMG_SHORTWAVE_TOLERANCE_WM2
from av_wrf.contracts import ReadyReceipt, SolverReceipt, parse_iso_utc
from av_wrf.contracts.receipts import req_int
from av_wrf.contracts.zarr_schema import (
    WRF_ZARR_V3_COORDS,
    WRF_ZARR_V3_FIELDS,
    ZARR_SCHEMA_ID,
    ZARR_SCHEMA_VERSION,
)
from av_wrf.runner import sha256_file
from av_wrf.runner.publish import ready_forecast_relpath

_LEGACY_ALBEDO_SENTINEL = -9999.0
_SHORTWAVE = ("SWDOWN", "SWNORM", "SWDNB", "SWUPB", "SWDNT", "SWUPT")


def _read_doc(path: Path) -> dict[str, object]:
    doc = json.loads(path.read_bytes())
    if not isinstance(doc, dict):
        raise ValueError(f"Expected JSON object: {path}")
    return doc


def _verify_hashes(root: Path, hashes: tuple[tuple[str, str], ...], actual: set[str]) -> None:
    expected = dict(hashes)
    if len(expected) != len(hashes) or set(expected) != actual:
        raise ValueError(f"Object inventory differs from receipt: {root}")
    for name, digest in hashes:
        path = root / name
        if not path.resolve().is_relative_to(root.resolve()) or sha256_file(path) != digest:
            raise ValueError(f"Object digest differs from receipt: {path}")


def _open_forecast(path: Path) -> tuple[ReadyReceipt, zarr.Group]:
    ready = ReadyReceipt.from_doc(_read_doc(path))
    if (ready.schema_id, ready.schema_version) != (ZARR_SCHEMA_ID, ZARR_SCHEMA_VERSION):
        raise ValueError(f"Unexpected forecast schema: {path}")
    ready_dir = path.parent.resolve()
    root = (ready_dir / ready_forecast_relpath(ready)).resolve()
    if root == ready_dir or not root.is_relative_to(ready_dir):
        raise ValueError(f"Forecast store escapes its ready receipt directory: {path}")
    _verify_hashes(
        root,
        ready.shard_sha256s,
        {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()},
    )
    group = zarr.open_group(root, mode="r")
    _verify_dimensions(ready, group)
    return ready, group


def _verify_dimensions(ready: ReadyReceipt, group: zarr.Group) -> None:
    fields = {spec.name for spec in WRF_ZARR_V3_FIELDS}
    coords = {spec.name for spec in WRF_ZARR_V3_COORDS}
    if set(ready.field_names) != fields or set(group.array_keys()) != fields | coords:
        raise ValueError("Unexpected forecast fields or coordinates")
    sizes = {
        "time": len(ready.lead_hours),
        "level": ready.exported_levels_k,
        "y": req_int(ready.grid, "height"),
        "x": req_int(ready.grid, "width"),
    }
    if min(sizes.values()) < 1:
        raise ValueError("Forecast dimensions must be positive")
    shapes: dict[str, tuple[int, ...]] = {
        coord.name: (sizes[coord.dim],) for coord in WRF_ZARR_V3_COORDS
    }
    shapes.update(
        (spec.name, tuple(sizes[dim] for dim in spec.dims)) for spec in WRF_ZARR_V3_FIELDS
    )
    for name, shape in shapes.items():
        if group.get_array(name).shape != shape:
            raise ValueError(f"Forecast array shape differs from the receipt: {name}")
    for key, values in (
        ("time", ready.lead_hours),
        ("valid_time_unix_s", _unix_times(ready.valid_times_utc)),
        ("window_start_unix_s", _unix_times(ready.window_starts_utc)),
        ("window_end_unix_s", _unix_times(ready.window_ends_utc)),
    ):
        if not np.array_equal(np.asarray(group.get_array(key)[:]), values):
            raise ValueError(f"Forecast coordinate {key} differs from its receipt")


def _unix_times(timestamps: tuple[str, ...]) -> tuple[int, ...]:
    return tuple(int(parse_iso_utc(timestamp).timestamp()) for timestamp in timestamps)


def _metrics(
    candidate: AnyArray, baseline: AnyArray | None, units: str, *, time_varying: bool
) -> dict[str, object]:
    if baseline is not None and baseline.shape != candidate.shape:
        raise ValueError(f"Forecast field shape differs: {candidate.path}")
    stats: dict[str, object] = {"units": units}
    total = 0.0
    maximum = -np.inf
    sum_diff = sum_abs = peak_diff = 0.0
    worst: tuple[int, ...] = ()
    lead_mae: list[float] = []
    frames: Iterable[int | None] = range(candidate.shape[0]) if time_varying else (None,)
    for index in frames:
        selection = slice(None) if index is None else index
        values = np.asarray(candidate[selection], dtype=np.float64)
        if not np.isfinite(values).all():
            raise ValueError(f"Nonfinite forecast field: {candidate.path} at index {index}")
        total += float(np.sum(values))
        maximum = max(maximum, float(values.max()))
        if baseline is not None:
            delta = values - np.asarray(baseline[selection], dtype=np.float64)
            if not np.isfinite(delta).all():
                raise ValueError(f"Nonfinite baseline field: {baseline.path} at index {index}")
            absolute = np.abs(delta)
            sum_diff += float(np.sum(delta))
            lead_total = float(np.sum(absolute))
            sum_abs += lead_total
            lead_mae.append(lead_total / values.size)
            value = float(absolute.max())
            if not worst or value > peak_diff:
                peak_diff = value
                cell = tuple(map(int, np.unravel_index(absolute.argmax(), absolute.shape)))
                worst = cell if index is None else (index, *cell)
    stats.update(mean=total / candidate.size, max=maximum)
    if baseline is not None:
        stats.update(
            mean_difference=sum_diff / candidate.size,
            mean_absolute_difference=sum_abs / candidate.size,
            max_absolute_difference=peak_diff,
            max_difference_index=worst,
        )
        if time_varying:
            stats["mean_absolute_difference_by_lead"] = lead_mae
    return stats


def _forecast_metrics(
    ready: ReadyReceipt, group: zarr.Group, baseline_path: Path | None
) -> dict[str, dict[str, object]]:
    baseline = None
    if baseline_path is not None:
        base_ready, baseline = _open_forecast(baseline_path)
        if base_ready.grid != ready.grid or any(
            not np.array_equal(
                np.asarray(baseline.get_array(key)[:]), np.asarray(group.get_array(key)[:])
            )
            for key in (*(spec.name for spec in WRF_ZARR_V3_COORDS), "terrain_m")
        ):
            raise ValueError("Compared forecasts must share the same grid and time coordinates")
    return {
        spec.name: _metrics(
            group.get_array(spec.name),
            None if baseline is None else baseline.get_array(spec.name),
            spec.units,
            time_varying=spec.dims[0] == "time",
        )
        for spec in WRF_ZARR_V3_FIELDS
    }


def _read_native(path: Path, index: int) -> dict[str, npt.NDArray[np.float64]]:
    with netCDF4.Dataset(path) as ds:
        result = {}
        for name in (*_SHORTWAVE, "ALBEDO"):
            if name not in ds.variables:
                raise ValueError(f"Missing native radiation variable {name}: {path}")
            values = np.ma.asarray(ds[name][index], dtype=np.float64)
            data = np.asarray(values.filled(np.nan))
            if data.ndim != 2 or (name != "ALBEDO" and not np.isfinite(data).all()):
                raise ValueError(f"Invalid native radiation variable {name}: {path}")
            result[name] = data
    return result


def _radiation_counts(fields: dict[str, npt.NDArray[np.float64]]) -> dict[str, int]:
    tol = RRTMG_SHORTWAVE_TOLERANCE_WM2
    # Albedo scales SWDNB at the column bottom; match the producer's daylight admission.
    lit = fields["SWDNB"] > tol
    albedo = fields["ALBEDO"]
    # RRTMG writes these two fluxes on the same plane at the bottom of its column.
    # https://github.com/wrf-model/WRF/blob/v4.8.0/phys/module_ra_rrtmg_sw.F#L10790-L10799
    excess = fields["SWUPB"] > fields["SWDNB"] + tol
    legacy = np.isclose(
        fields["SWUPB"],
        fields["SWDNB"] * _LEGACY_ALBEDO_SENTINEL,
        atol=tol,
        rtol=2.0e-6,
    )
    return {
        "daylight_samples": int(lit.sum()),
        "invalid_daylight_albedo": int(
            (lit & (~np.isfinite(albedo) | (albedo < 0) | (albedo > 1))).sum()
        ),
        "excess_surface_reflection": int(excess.sum()),
        "legacy_albedo_reflection": int((lit & legacy).sum()),
        **{f"negative_{name}": int((fields[name] < -tol).sum()) for name in _SHORTWAVE},
    }


def _interior(
    fields: dict[str, npt.NDArray[np.float64]], shape: tuple[int, ...]
) -> dict[str, npt.NDArray[np.float64]]:
    raw_y, raw_x = fields["ALBEDO"].shape
    ny, nx = shape
    dy, dx = raw_y - ny, raw_x - nx
    if min(dy, dx) < 0 or dy % 2 or dx % 2:
        raise ValueError("Published grid must be a symmetric crop of the native mass grid")
    return {
        name: arr[dy // 2 : dy // 2 + ny, dx // 2 : dx // 2 + nx] for name, arr in fields.items()
    }


def _native_radiation(
    ready: ReadyReceipt, group: zarr.Group, directory: Path, receipt_path: Path
) -> dict[str, object]:
    solver = SolverReceipt.from_doc(_read_doc(receipt_path))
    if (solver.cycle_partition, solver.attempt_id, solver.config_sha256, solver.oci_digest) != (
        ready.cycle_partition,
        ready.attempt_id,
        ready.config_sha256,
        ready.oci_digest,
    ):
        raise ValueError("Solver receipt and published forecast identities differ")
    # The receipt lists only the active domain's history, including on nested runs.
    first_name = solver.wrfout_sha256s[0][0] if solver.wrfout_sha256s else ""
    match = re.fullmatch(r"wrfout_d([0-9]{2})_.+", first_name)
    if match is None:
        raise ValueError("Solver receipt lacks a WRF history filename")
    domain_id = int(match[1])
    pattern = f"wrfout_{domain_tag(domain_id)}_*"
    _verify_hashes(
        directory,
        solver.wrfout_sha256s,
        {p.name for p in directory.glob(pattern) if p.is_file()},
    )
    initial, leads = select_cycle_wrfout_slices(
        directory, ready.publish_origin_utc, ready.valid_times_utc, domain_id=domain_id
    )
    full: Counter[str] = Counter()
    interior: Counter[str] = Counter()
    frames = {}
    timestamps = (ready.publish_origin_utc, *ready.valid_times_utc)
    for valid, (path, index) in zip(timestamps, (initial, *leads), strict=True):
        fields = _read_native(path, index)
        counts = _radiation_counts(fields)
        full.update(counts)
        inside = _radiation_counts(_interior(fields, group.get_array("terrain_m").shape))
        interior.update(inside)
        frames[valid] = {"native": counts, "published_interior": inside}
    return {
        "native": dict(full),
        "published_interior": dict(interior),
        "per_frame": frames,
        "status": "fail" if any(v for k, v in full.items() if k != "daylight_samples") else "pass",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ready", type=Path, help="Candidate _ready.json")
    parser.add_argument("--baseline", type=Path, help="_ready.json on the same grid and times")
    parser.add_argument("--wrfout-dir", type=Path)
    parser.add_argument("--solver-receipt", type=Path)
    parser.add_argument("--require-clean-radiation", action="store_true")
    parser.add_argument("--output", type=Path, help="Write JSON to a file instead of stdout")
    args = parser.parse_args(argv)
    if (args.wrfout_dir is None) != (args.solver_receipt is None) or (
        args.require_clean_radiation and args.wrfout_dir is None
    ):
        parser.error("Native radiation audit requires both --wrfout-dir and --solver-receipt")
    ready, group = _open_forecast(args.ready)
    report: dict[str, object] = {
        "ready": str(args.ready),
        "baseline": str(args.baseline) if args.baseline else None,
        "valid_times_utc": ready.valid_times_utc,
        "verified_objects": len(ready.shard_sha256s),
        "fields": _forecast_metrics(ready, group, args.baseline),
    }
    failed = False
    if args.wrfout_dir is not None:
        radiation = _native_radiation(ready, group, args.wrfout_dir, args.solver_receipt)
        report["radiation"] = radiation
        failed = radiation["status"] == "fail"
    serialized = json.dumps(report, indent=2, allow_nan=False) + "\n"
    if args.output is None:
        print(serialized, end="")
    else:
        args.output.write_text(serialized, encoding="utf-8")
    return int(args.require_clean_radiation and failed)


__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
