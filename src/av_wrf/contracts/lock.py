"""Immutable cycle window, UTC parsing, accumulator baseline, and RunLock schemas."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from av_wrf.contracts.quantities import PubBucket2D, PubMass2D
from av_wrf.contracts.receipts import (
    DiagnosticsReceipt,
    ForcingManifest,
    ReadyReceipt,
    SolverReceipt,
    StagedPostManifest,
    req_int,
    req_int_tuple,
    req_pair_tuple,
    req_str,
)


def parse_iso_utc(iso_str: str) -> datetime:
    """Parse an explicit-offset ISO-8601 string into UTC; reject timezone-naive inputs."""
    cleaned = iso_str.strip()
    if not cleaned:
        raise ValueError("Empty UTC timestamp string")
    try:
        dt = datetime.fromisoformat(cleaned[:-1] + "Z" if cleaned.endswith("z") else cleaned)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO-8601 timestamp {iso_str!r}; pass explicit Z/offset") from exc
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
    """Published-grid cumulative signed heat-flux and bucketed rainfall state from f000."""

    valid_time_utc: str
    valid_time_unix_s: int
    achfx_j_m2: PubMass2D
    aclhf_j_m2: PubMass2D
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
            req_str(doc, "oci_digest"),
            req_str(doc, "wrf_git_sha"),
            req_str(doc, "wps_git_sha"),
            req_pair_tuple(doc, "patch_sha256s"),
            req_str(doc, "compiler_version"),
            req_str(doc, "mpi_version"),
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
    producer_sha256: str | None = None

    def to_doc(self) -> dict[str, object]:
        return dataclasses.asdict(self)

    @classmethod
    def from_doc(cls, doc: Mapping[str, object]) -> RunLock:
        bm_raw = doc.get("build_manifest")
        if not isinstance(bm_raw, Mapping):
            raise ValueError("Expected mapping for 'build_manifest'")
        return cls(
            req_str(doc, "product_id"),
            req_str(doc, "model_id"),
            req_str(doc, "cycle_partition"),
            req_str(doc, "attempt_id"),
            req_str(doc, "forcing_cycle_utc"),
            req_str(doc, "wrf_init_utc"),
            req_str(doc, "publish_origin_utc"),
            req_int(doc, "spinup_hours"),
            req_int_tuple(doc, "lead_hours"),
            req_str(doc, "forcing_profile_id"),
            req_str(doc, "domain_profile_id"),
            req_str(doc, "science_profile_id"),
            req_str(doc, "output_profile_id"),
            req_str(doc, "config_sha256"),
            req_str(doc, "geog_bundle_sha256"),
            BuildManifest.from_doc(bm_raw),
            req_int(doc, "mpi_ranks"),
            req_str(doc, "producer_sha256") if doc.get("producer_sha256") is not None else None,
        )


__all__ = [
    "AccumBaseline",
    "BuildManifest",
    "CycleWindow",
    "DiagnosticsReceipt",
    "ForcingManifest",
    "ReadyReceipt",
    "RunLock",
    "SolverReceipt",
    "StagedPostManifest",
    "format_iso_utc",
    "parse_iso_utc",
]
