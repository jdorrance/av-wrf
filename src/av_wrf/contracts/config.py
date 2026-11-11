"""Frozen declarative configuration dataclasses."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DomainProfile:
    profile_id: str
    earth_radius_m: float
    center_lat_deg: float
    center_lon_deg: float
    std_parallel_1_deg: float
    std_parallel_2_deg: float
    central_meridian_deg: float
    dx_m: float
    dy_m: float
    e_we: int
    e_sn: int
    bdy_trim_cells: int
    e_vert: int
    exported_levels_k: int
    p_top_pa: float
    auto_levels_opt: int
    dzbot_m: float
    dzstretch_s: float
    dzstretch_u: float
    max_dz_m: float
    min_bl_levels_2km_agl: int
    terrain_elevations_m: tuple[float, ...]
    dem_source: str
    landuse_source: str
    geog_data_path: str


@dataclass(frozen=True)
class ScienceProfile:
    profile_id: str
    wrf_version: str
    wps_version: str
    mp_physics: int
    ra_lw_physics: int
    ra_sw_physics: int
    radt_min: int
    slope_rad: int
    topo_shading: int
    shadlen_m: float
    sf_surface_physics: int
    opt_runsrf: int
    opt_runsub: int
    bl_pbl_physics: int
    sf_sfclay_physics: int
    topo_wind: int
    ysu_topdown_pblmix: int
    shinhong_scu_mixing: bool
    shinhong_nonlocal_flux: bool
    cu_physics: int
    gwd_opt: int
    gwd_if_nonhyd: bool
    use_theta_m: int
    base_temp_k: float
    hybrid_opt: int
    etac: float
    diff_opt: int
    km_opt: int
    diff_6th_opt: int
    diff_6th_factor: float
    epssm: float
    w_damping: int
    damp_opt: int
    zdamp_m: float
    dampcoef: float
    time_step_s: int
    spec_bdy_width: int
    spec_zone: int
    relax_zone: int
    interval_seconds: int
    nwp_diagnostics: int
    afwa_diag_opt: int
    afwa_severe_opt: int
    afwa_buoy_opt: int
    history_interval_min: int
    bucket_mm: float
    bucket_j: float


@dataclass(frozen=True)
class ForcingProfile:
    profile_id: str
    provider: str
    grid_spacing_deg: float
    pressure_level_count: int
    soil_layer_centers_cm: tuple[float, ...]
    has_dedicated_sst: bool
    source_ready_lag_p95_hours: float
    mirrors: tuple[str, ...]
    fail_closed: bool


@dataclass(frozen=True)
class OutputProfile:
    profile_id: str
    schema_id: str
    map_end_hour: int
    hourly_through_hour: int
    tail_step_hours: int
    shard_time_steps: int
    inner_chunk_yx: tuple[int, int]
    zstd_level: int


@dataclass(frozen=True)
class ProductSpec:
    product_id: str
    model_id: str
    label: str
    region_tz: str
    domain_profile: str
    science_profile: str
    primary_source_profile: str
    candidate_source_profile: str
    output_profile: str
    spinup_hours: int
    forcing_cycle_hour_utc: int
    publish_origin_hour_utc: int
    local_deadline_hhmm: str
    min_horizon_hours: int
    max_cycle_age_hours: int
    schedule_cron_utc: str


@dataclass(frozen=True)
class ProductCheckSummary:
    product_id: str
    published_yx: tuple[int, int]
    x0_m: float
    y0_m: float
    primary_source: str
    candidate_source: str
    lead_count: int
    bl_levels_2km: int
    remaining_horizon_floor_hours: int
    time_step_s: int
    zarr_format: int


__all__ = [
    "DomainProfile",
    "ForcingProfile",
    "OutputProfile",
    "ProductCheckSummary",
    "ProductSpec",
    "ScienceProfile",
]
