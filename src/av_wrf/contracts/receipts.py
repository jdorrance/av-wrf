"""Typed JSON decoders and immutable forcing, solver, post, publication, and diag receipts."""

from __future__ import annotations

import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass


def req_str(doc: Mapping[str, object], key: str) -> str:
    val = doc.get(key)
    if not isinstance(val, str) or not val:
        raise ValueError(f"Expected non-empty string for '{key}'")
    return val


def req_int(doc: Mapping[str, object], key: str) -> int:
    val = doc.get(key)
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(f"Expected int for '{key}'")
    return val


def req_float(doc: Mapping[str, object], key: str) -> float:
    val = doc.get(key)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"Expected number for '{key}'")
    return float(val)


def req_int_tuple(doc: Mapping[str, object], key: str) -> tuple[int, ...]:
    val = doc.get(key)
    if not isinstance(val, (list, tuple)) or any(
        isinstance(x, bool) or not isinstance(x, int) for x in val
    ):
        raise ValueError(f"Expected int list for '{key}'")
    return tuple(val)


def req_str_tuple(doc: Mapping[str, object], key: str) -> tuple[str, ...]:
    val = doc.get(key)
    if not isinstance(val, (list, tuple)) or any(not isinstance(x, str) for x in val):
        raise ValueError(f"Expected string list for '{key}'")
    return tuple(val)


def req_pair_tuple(doc: Mapping[str, object], key: str) -> tuple[tuple[str, str], ...]:
    val = doc.get(key)
    if not isinstance(val, (list, tuple)):
        raise ValueError(f"Expected pair list for '{key}'")
    return tuple(_sha_pair(item, key) for item in val)


def _sha_pair(item: object, key: str) -> tuple[str, str]:
    if (
        not isinstance(item, (list, tuple))
        or len(item) != 2
        or not isinstance(item[0], str)
        or not isinstance(item[1], str)
    ):
        raise ValueError(f"Invalid [key, sha256] pair in '{key}'")
    return item[0], item[1]


def _stage_seconds(doc: Mapping[str, object]) -> tuple[tuple[str, float], ...]:
    raw = doc.get("stage_seconds")
    if not isinstance(raw, (list, tuple)):
        raise ValueError("Expected stage_seconds list")
    stages: list[tuple[str, float]] = []
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            raise ValueError("Invalid stage_seconds entry")
        name, seconds = item
        if (
            not isinstance(name, str)
            or not name
            or isinstance(seconds, bool)
            or not isinstance(seconds, (int, float))
            or not 0 <= seconds < math.inf
        ):
            raise ValueError("Invalid stage_seconds entry")
        stages.append((name, float(seconds)))
    return tuple(stages)


@dataclass(frozen=True)
class ForcingManifest:
    """Verified forcing inventory binding source profile, cycle, forecast hours, and SHA-256s."""

    product_id: str
    cycle_partition: str
    forcing_profile_id: str
    forcing_cycle_utc: str
    vtable_name: str
    interval_seconds: int
    forecast_hours: tuple[int, ...]
    file_sha256s: tuple[tuple[str, str], ...]
    acquisition_inventory: tuple[str, str] | None = None

    def to_doc(self) -> dict[str, object]:
        doc = dataclasses.asdict(self)
        if self.acquisition_inventory is None:
            del doc["acquisition_inventory"]
        return doc

    @classmethod
    def from_doc(cls, doc: Mapping[str, object]) -> ForcingManifest:
        return cls(
            req_str(doc, "product_id"),
            req_str(doc, "cycle_partition"),
            req_str(doc, "forcing_profile_id"),
            req_str(doc, "forcing_cycle_utc"),
            req_str(doc, "vtable_name"),
            req_int(doc, "interval_seconds"),
            req_int_tuple(doc, "forecast_hours"),
            req_pair_tuple(doc, "file_sha256s"),
            _sha_pair(doc["acquisition_inventory"], "acquisition_inventory")
            if "acquisition_inventory" in doc
            else None,
        )


@dataclass(frozen=True)
class SolverReceipt:
    """Cryptographic proof binding executed WPS/WRF inputs, geography, and wrfout files to lock."""

    product_id: str
    cycle_partition: str
    attempt_id: str
    config_sha256: str
    oci_digest: str
    geog_sha256: str
    forcing_sha256s: tuple[tuple[str, str], ...]
    wrfout_sha256s: tuple[tuple[str, str], ...]
    acquisition_inventory_sha256: str | None = None
    stage_seconds: tuple[tuple[str, float], ...] = ()

    def to_doc(self) -> dict[str, object]:
        doc = dataclasses.asdict(self)
        if self.acquisition_inventory_sha256 is None:
            del doc["acquisition_inventory_sha256"]
        if not self.stage_seconds:
            del doc["stage_seconds"]
        return doc

    @classmethod
    def from_doc(cls, doc: Mapping[str, object]) -> SolverReceipt:
        return cls(
            req_str(doc, "product_id"),
            req_str(doc, "cycle_partition"),
            req_str(doc, "attempt_id"),
            req_str(doc, "config_sha256"),
            req_str(doc, "oci_digest"),
            req_str(doc, "geog_sha256"),
            req_pair_tuple(doc, "forcing_sha256s"),
            req_pair_tuple(doc, "wrfout_sha256s"),
            req_str(doc, "acquisition_inventory_sha256")
            if "acquisition_inventory_sha256" in doc
            else None,
            _stage_seconds(doc) if "stage_seconds" in doc else (),
        )


@dataclass(frozen=True)
class StagedPostManifest:
    """Validated attempt-scoped post manifest consumed by strict `publish` admission."""

    zarr_relpath: str
    lead_hours: tuple[int, ...]
    valid_times_utc: tuple[str, ...]
    meta_sha256s: tuple[tuple[str, str], ...]
    shard_sha256s: tuple[tuple[str, str], ...]
    diagnostics_sha256: str
    execution_mode: str = "solver"
    producer_sha256: str | None = None

    def to_doc(self) -> dict[str, object]:
        doc = dataclasses.asdict(self)
        if self.producer_sha256 is None:
            del doc["producer_sha256"]
        return doc

    @classmethod
    def from_doc(cls, doc: Mapping[str, object]) -> StagedPostManifest:
        mode = req_str(doc, "execution_mode") if "execution_mode" in doc else "solver"
        if mode not in ("solver", "synthetic"):
            raise ValueError(f"Invalid execution_mode in StagedPostManifest: {mode}")
        return cls(
            req_str(doc, "zarr_relpath"),
            req_int_tuple(doc, "lead_hours"),
            req_str_tuple(doc, "valid_times_utc"),
            req_pair_tuple(doc, "meta_sha256s"),
            req_pair_tuple(doc, "shard_sha256s"),
            req_str(doc, "diagnostics_sha256"),
            mode,
            req_str(doc, "producer_sha256") if "producer_sha256" in doc else None,
        )


@dataclass(frozen=True)
class ReadyReceipt:
    """Complete cycle publication receipt consumed by windy-av-layer Zarr v3 provider."""

    schema_id: str
    schema_version: int
    model_id: str
    cycle_partition: str
    publish_origin_utc: str
    attempt_id: str
    zarr_relpath: str
    grid: dict[str, int | float | str | bool]
    exported_levels_k: int
    lead_hours: tuple[int, ...]
    valid_times_utc: tuple[str, ...]
    window_starts_utc: tuple[str, ...]
    window_ends_utc: tuple[str, ...]
    field_names: tuple[str, ...]
    shard_sha256s: tuple[tuple[str, str], ...]
    config_sha256: str
    forcing_profile_id: str
    oci_digest: str

    def to_doc(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    @classmethod
    def from_doc(cls, doc: Mapping[str, object]) -> ReadyReceipt:
        raw_grid = doc.get("grid")
        if not isinstance(raw_grid, Mapping):
            raise ValueError("Expected mapping for 'grid'")
        grid_out: dict[str, int | float | str | bool] = {}
        for gk, gv in raw_grid.items():
            if not isinstance(gk, str) or not isinstance(gv, (int, float, str, bool)):
                raise ValueError("Invalid grid entry in ReadyReceipt")
            grid_out[gk] = gv
        return cls(
            req_str(doc, "schema_id"),
            req_int(doc, "schema_version"),
            req_str(doc, "model_id"),
            req_str(doc, "cycle_partition"),
            req_str(doc, "publish_origin_utc"),
            req_str(doc, "attempt_id"),
            req_str(doc, "zarr_relpath"),
            grid_out,
            req_int(doc, "exported_levels_k"),
            req_int_tuple(doc, "lead_hours"),
            req_str_tuple(doc, "valid_times_utc"),
            req_str_tuple(doc, "window_starts_utc"),
            req_str_tuple(doc, "window_ends_utc"),
            req_str_tuple(doc, "field_names"),
            req_pair_tuple(doc, "shard_sha256s"),
            req_str(doc, "config_sha256"),
            req_str(doc, "forcing_profile_id"),
            req_str(doc, "oci_digest"),
        )


@dataclass(frozen=True)
class DiagnosticsReceipt:
    """Runtime metrics and grid-cell means. `max_cfl` is an output-frame Courant proxy."""

    model_id: str
    cycle_partition: str
    attempt_id: str
    stage_seconds: tuple[tuple[str, float], ...]
    max_cfl: float
    initial_dps_dt_hpa_hr: tuple[float, ...]
    mean_sensible_flux_wm2: float
    mean_latent_flux_wm2: float
    total_precip_mm: float

    def to_doc(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    @classmethod
    def from_doc(cls, doc: Mapping[str, object]) -> DiagnosticsReceipt:
        raw_dps = doc.get("initial_dps_dt_hpa_hr")
        if not isinstance(raw_dps, (list, tuple)) or any(
            isinstance(x, bool) or not isinstance(x, (int, float)) for x in raw_dps
        ):
            raise ValueError("Expected float list for 'initial_dps_dt_hpa_hr'")
        return cls(
            req_str(doc, "model_id"),
            req_str(doc, "cycle_partition"),
            req_str(doc, "attempt_id"),
            _stage_seconds(doc),
            req_float(doc, "max_cfl"),
            tuple(float(x) for x in raw_dps),
            req_float(doc, "mean_sensible_flux_wm2"),
            req_float(doc, "mean_latent_flux_wm2"),
            req_float(doc, "total_precip_mm"),
        )
