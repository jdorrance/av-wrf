"""Immutable cycle window, accumulator baseline, lock, checkpoint, and receipt schemas."""

from __future__ import annotations

from dataclasses import dataclass

from av_wrf.contracts.quantities import PubMass2D


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
    i_achfx: tuple[int, ...]
    aclhf_j_m2: PubMass2D
    i_aclhf: tuple[int, ...]
    rainc_mm: PubMass2D
    i_rainc: tuple[int, ...]
    rainnc_mm: PubMass2D
    i_rainnc: tuple[int, ...]


@dataclass(frozen=True)
class BuildManifest:
    oci_digest: str
    wrf_git_sha: str
    wps_git_sha: str
    patch_sha256s: tuple[tuple[str, str], ...]
    compiler_version: str
    mpi_version: str


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


@dataclass(frozen=True)
class CheckpointManifest:
    attempt_id: str
    valid_time_utc: str
    oci_digest: str
    mpi_ranks: int
    wrfrst_sha256s: tuple[tuple[str, str], ...]
    accum_baseline_sha256: str


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


@dataclass(frozen=True)
class DiagnosticsReceipt:
    model_id: str
    cycle_partition: str
    attempt_id: str
    stage_seconds: tuple[tuple[str, float], ...]
    max_cfl: float
    spinup_dps_dt_hpa_hr: tuple[float, ...]
    mean_sensible_flux_wm2: float
    mean_latent_flux_wm2: float
    total_precip_mm: float


__all__ = [
    "AccumBaseline",
    "BuildManifest",
    "CheckpointManifest",
    "CycleWindow",
    "DiagnosticsReceipt",
    "ReadyReceipt",
    "RunLock",
]
