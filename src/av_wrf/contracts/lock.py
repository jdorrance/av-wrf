"""Immutable cycle window, UTC parsing, accumulator baseline, lock, and receipt schemas."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from av_wrf.contracts.quantities import PubBucket2D, PubMass2D


def parse_iso_utc(iso_str: str) -> datetime:
    """Parse an explicit-offset ISO-8601 string into UTC; reject timezone-naive inputs."""
    cleaned = iso_str.strip()
    if not cleaned:
        raise ValueError("Empty UTC timestamp string")
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(
            f"Timezone-naive timestamp is forbidden; pass explicit Z/offset: {iso_str}"
        )
    return dt.astimezone(UTC)


def format_iso_utc(dt: datetime) -> str:
    """Format an aware datetime as canonical YYYY-MM-DDTHH:MM:SSZ."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("Cannot format timezone-naive datetime as UTC")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _req_str(doc: Mapping[str, object], key: str) -> str:
    val = doc.get(key)
    if not isinstance(val, str) or not val:
        raise ValueError(f"Expected non-empty string for '{key}'")
    return val


def _req_int(doc: Mapping[str, object], key: str) -> int:
    val = doc.get(key)
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(f"Expected int for '{key}'")
    return val


def _req_float(doc: Mapping[str, object], key: str) -> float:
    val = doc.get(key)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"Expected number for '{key}'")
    return float(val)


def _req_int_tuple(doc: Mapping[str, object], key: str) -> tuple[int, ...]:
    val = doc.get(key)
    if not isinstance(val, (list, tuple)) or any(
        isinstance(x, bool) or not isinstance(x, int) for x in val
    ):
        raise ValueError(f"Expected int list for '{key}'")
    return tuple(val)


def _req_str_tuple(doc: Mapping[str, object], key: str) -> tuple[str, ...]:
    val = doc.get(key)
    if not isinstance(val, (list, tuple)) or any(not isinstance(x, str) for x in val):
        raise ValueError(f"Expected string list for '{key}'")
    return tuple(val)


def _req_pair_tuple(doc: Mapping[str, object], key: str) -> tuple[tuple[str, str], ...]:
    val = doc.get(key)
    if not isinstance(val, (list, tuple)):
        raise ValueError(f"Expected pair list for '{key}'")
    pairs: list[tuple[str, str]] = []
    for item in val:
        if (
            not isinstance(item, (list, tuple))
            or len(item) != 2
            or not isinstance(item[0], str)
            or not isinstance(item[1], str)
        ):
            raise ValueError(f"Invalid [key, sha256] pair in '{key}'")
        pairs.append((item[0], item[1]))
    return tuple(pairs)


@dataclass(frozen=True)
class CycleWindow:
    forcing_cycle_utc: str
    wrf_init_utc: str
    publish_origin_utc: str
    cycle_partition: str
    lead_hours: tuple[int, ...]
    valid_times_utc: tuple[str, ...]
    window_starts_utc: tuple[str, ...]


@dataclass(frozen=True)
class AccumBaseline:
    """Published-grid cumulative energy/precip state persisted from f000 and across restarts."""

    valid_time_utc: str
    valid_time_unix_s: int
    achfx_j_m2: PubMass2D
    i_achfx: PubBucket2D
    aclhf_j_m2: PubMass2D
    i_aclhf: PubBucket2D
    rainc_mm: PubMass2D
    i_rainc: PubBucket2D
    rainnc_mm: PubMass2D
    i_rainnc: PubBucket2D


@dataclass(frozen=True)
class BuildManifest:
    oci_digest: str
    wrf_git_sha: str
    wps_git_sha: str
    patch_sha256s: tuple[tuple[str, str], ...]
    compiler_version: str
    mpi_version: str

    def to_doc(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    @classmethod
    def from_doc(cls, doc: Mapping[str, object]) -> BuildManifest:
        return cls(
            oci_digest=_req_str(doc, "oci_digest"),
            wrf_git_sha=_req_str(doc, "wrf_git_sha"),
            wps_git_sha=_req_str(doc, "wps_git_sha"),
            patch_sha256s=_req_pair_tuple(doc, "patch_sha256s"),
            compiler_version=_req_str(doc, "compiler_version"),
            mpi_version=_req_str(doc, "mpi_version"),
        )


@dataclass(frozen=True)
class RunLock:
    product_id: str
    model_id: str
    cycle_partition: str
    attempt_id: str
    forcing_cycle_utc: str
    wrf_init_utc: str
    publish_origin_utc: str
    spinup_hours: int
    lead_hours: tuple[int, ...]
    forcing_profile_id: str
    domain_profile_id: str
    science_profile_id: str
    output_profile_id: str
    config_sha256: str
    geog_bundle_sha256: str
    build_manifest: BuildManifest
    mpi_ranks: int

    def to_doc(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    @classmethod
    def from_doc(cls, doc: Mapping[str, object]) -> RunLock:
        bm_raw = doc.get("build_manifest")
        if not isinstance(bm_raw, Mapping):
            raise ValueError("Expected mapping for 'build_manifest'")
        return cls(
            product_id=_req_str(doc, "product_id"),
            model_id=_req_str(doc, "model_id"),
            cycle_partition=_req_str(doc, "cycle_partition"),
            attempt_id=_req_str(doc, "attempt_id"),
            forcing_cycle_utc=_req_str(doc, "forcing_cycle_utc"),
            wrf_init_utc=_req_str(doc, "wrf_init_utc"),
            publish_origin_utc=_req_str(doc, "publish_origin_utc"),
            spinup_hours=_req_int(doc, "spinup_hours"),
            lead_hours=_req_int_tuple(doc, "lead_hours"),
            forcing_profile_id=_req_str(doc, "forcing_profile_id"),
            domain_profile_id=_req_str(doc, "domain_profile_id"),
            science_profile_id=_req_str(doc, "science_profile_id"),
            output_profile_id=_req_str(doc, "output_profile_id"),
            config_sha256=_req_str(doc, "config_sha256"),
            geog_bundle_sha256=_req_str(doc, "geog_bundle_sha256"),
            build_manifest=BuildManifest.from_doc(bm_raw),
            mpi_ranks=_req_int(doc, "mpi_ranks"),
        )


@dataclass(frozen=True)
class CheckpointManifest:
    attempt_id: str
    valid_time_utc: str
    oci_digest: str
    mpi_ranks: int
    wrfrst_sha256s: tuple[tuple[str, str], ...]
    accum_baseline_sha256: str


@dataclass(frozen=True)
class StagedPostManifest:
    """Validated attempt-scoped post manifest consumed by strict `publish` admission."""

    zarr_relpath: str
    lead_hours: tuple[int, ...]
    valid_times_utc: tuple[str, ...]
    meta_sha256s: tuple[tuple[str, str], ...]
    shard_sha256s: tuple[tuple[str, str], ...]
    diagnostics_sha256: str

    def to_doc(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    @classmethod
    def from_doc(cls, doc: Mapping[str, object]) -> StagedPostManifest:
        return cls(
            zarr_relpath=_req_str(doc, "zarr_relpath"),
            lead_hours=_req_int_tuple(doc, "lead_hours"),
            valid_times_utc=_req_str_tuple(doc, "valid_times_utc"),
            meta_sha256s=_req_pair_tuple(doc, "meta_sha256s"),
            shard_sha256s=_req_pair_tuple(doc, "shard_sha256s"),
            diagnostics_sha256=_req_str(doc, "diagnostics_sha256"),
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
            schema_id=_req_str(doc, "schema_id"),
            schema_version=_req_int(doc, "schema_version"),
            model_id=_req_str(doc, "model_id"),
            cycle_partition=_req_str(doc, "cycle_partition"),
            publish_origin_utc=_req_str(doc, "publish_origin_utc"),
            attempt_id=_req_str(doc, "attempt_id"),
            zarr_relpath=_req_str(doc, "zarr_relpath"),
            grid=grid_out,
            exported_levels_k=_req_int(doc, "exported_levels_k"),
            lead_hours=_req_int_tuple(doc, "lead_hours"),
            valid_times_utc=_req_str_tuple(doc, "valid_times_utc"),
            window_starts_utc=_req_str_tuple(doc, "window_starts_utc"),
            window_ends_utc=_req_str_tuple(doc, "window_ends_utc"),
            field_names=_req_str_tuple(doc, "field_names"),
            shard_sha256s=_req_pair_tuple(doc, "shard_sha256s"),
            config_sha256=_req_str(doc, "config_sha256"),
            forcing_profile_id=_req_str(doc, "forcing_profile_id"),
            oci_digest=_req_str(doc, "oci_digest"),
        )


@dataclass(frozen=True)
class DiagnosticsReceipt:
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
        raw_stages = doc.get("stage_seconds")
        if not isinstance(raw_stages, (list, tuple)):
            raise ValueError("Expected stage_seconds list")
        stages: list[tuple[str, float]] = []
        for item in raw_stages:
            if (
                not isinstance(item, (list, tuple))
                or len(item) != 2
                or not isinstance(item[0], str)
                or isinstance(item[1], bool)
                or not isinstance(item[1], (int, float))
            ):
                raise ValueError("Invalid stage_seconds entry")
            stages.append((item[0], float(item[1])))
        raw_dps = doc.get("initial_dps_dt_hpa_hr")
        if not isinstance(raw_dps, (list, tuple)) or any(
            isinstance(x, bool) or not isinstance(x, (int, float)) for x in raw_dps
        ):
            raise ValueError("Expected float list for 'initial_dps_dt_hpa_hr'")
        return cls(
            model_id=_req_str(doc, "model_id"),
            cycle_partition=_req_str(doc, "cycle_partition"),
            attempt_id=_req_str(doc, "attempt_id"),
            stage_seconds=tuple(stages),
            max_cfl=_req_float(doc, "max_cfl"),
            initial_dps_dt_hpa_hr=tuple(float(x) for x in raw_dps),
            mean_sensible_flux_wm2=_req_float(doc, "mean_sensible_flux_wm2"),
            mean_latent_flux_wm2=_req_float(doc, "mean_latent_flux_wm2"),
            total_precip_mm=_req_float(doc, "total_precip_mm"),
        )


__all__ = [
    "AccumBaseline",
    "BuildManifest",
    "CheckpointManifest",
    "CycleWindow",
    "DiagnosticsReceipt",
    "ReadyReceipt",
    "RunLock",
    "StagedPostManifest",
    "format_iso_utc",
    "parse_iso_utc",
]
