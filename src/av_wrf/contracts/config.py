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
    # WPS GEOGRID.TBL.ARW field-specific tokens; `default` silently selects the coarse
    # 10-arcminute (18.5 km) LAI/VAR_SSO tables even when 30s tiles are staged.
    lai_source: str = "modis_lai"
    varsso_source: str = "30s"
    # 1-way concentric parent buffer: d01 covers the child plus `parent_halo_cells` coarse
    # cells on every side, so a fine nest is never forced directly from a >3.5x source grid.
    parent_grid_ratio: int = 1
    parent_halo_cells: int = 0
    nest_trigger_ratio: float = 3.5


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
    afwa_diag_opt: int
    afwa_buoy_opt: int
    history_interval_min: int
    bucket_mm: float
    # MYNN2.5-EDMF subgrid clouds and lake physics replace the YSU-only defaults; when
    # bl_pbl_physics=5 the matching surface layer is 5 and topo_wind/ysu_topdown_pblmix are 0.
    bl_mynn_edmf: int = 0
    icloud: int = 1
    icloud_bl: int = 0
    sf_lake_physics: int = 0
    use_lakedepth: int = 0
    lakedepth_default_m: float = 50.0
    lake_min_elev_m: float = 5.0
    # Tilted-eta 6th-order filtering mixes theta/qv across valley inversions without the
    # slope-aware limiter (WRF phys/module_diffusion.F).
    diff_6th_slopeopt: int = 0
    diff_6th_thresh: float = 0.1
    # WRF adaptive RK3 stepping (dyn_em/module_em.F); caps wall-clock on calm days.
    use_adaptive_time_step: bool = False
    target_cfl: float = 1.2
    target_hcfl: float = 0.84
    min_time_step_s: int = 6
    max_time_step_s: int = 17


@dataclass(frozen=True)
class ForcingProfile:
    profile_id: str
    provider: str
    vtable_name: str
    interval_seconds: int
    grid_spacing_deg: float
    metgrid_levels: int
    metgrid_soil_levels: int
    source_ready_lag_p95_hours: float
    mirrors: tuple[str, ...]
    fail_closed: bool
    use_sh_qv: bool = False


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
    candidate_recipe_available: bool
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
