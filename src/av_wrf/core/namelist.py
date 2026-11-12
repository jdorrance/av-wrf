"""Compile WRF v4.8 and WPS v4.7 Fortran namelists from validated profiles."""

from __future__ import annotations

from datetime import timedelta

from av_wrf.contracts import (
    CycleWindow,
    DomainProfile,
    ForcingProfile,
    RunLock,
    ScienceProfile,
    parse_iso_utc,
)
from av_wrf.core.grid import computational_latlon_bounds, lambert_map_scale, validate_domain_profile

NamelistScalar = int | float | str | bool
NamelistSections = dict[str, dict[str, NamelistScalar]]


def _validate_dynamics(science: ScienceProfile, domain: DomainProfile) -> None:
    if science.spec_bdy_width != science.spec_zone + science.relax_zone:
        raise ValueError("spec_bdy_width must equal spec_zone + relax_zone")
    if domain.bdy_trim_cells <= science.spec_bdy_width:
        raise ValueError("bdy_trim_cells must exceed spec_bdy_width")
    # https://www2.mmm.ucar.edu/wrf/users/docs/technote/v4_technote.pdf
    # Lambert scale has a single latitude minimum. Its maximum is at a footprint extreme.
    _, south, _, north = computational_latlon_bounds(domain, halo_deg=0.0, snap_deg=0.001)
    m_max = max(
        lambert_map_scale(
            lat,
            std_parallel_1_deg=domain.std_parallel_1_deg,
            std_parallel_2_deg=domain.std_parallel_2_deg,
            origin_lat_deg=domain.center_lat_deg,
            radius_m=domain.earth_radius_m,
        )
        for lat in (south, north)
    )
    min_phys_dx_km = (domain.dx_m / m_max) / 1000.0
    max_dt_s = 6.0 * min_phys_dx_km
    if not 0 < science.time_step_s <= max_dt_s:
        raise ValueError(
            f"time_step_s ({science.time_step_s}) exceeds 6*dx_phys ({max_dt_s:.2f}s, "
            f"m_max={m_max:.4f})"
        )


def _validate_physics_compatibility(science: ScienceProfile) -> None:
    # https://github.com/wrf-model/WRF/wiki/WRF-V4.8.0-Known-Problems
    if science.sf_surface_physics == 4 and science.opt_runsrf == 5:
        raise ValueError("WRF v4.8 Noah-MP forbids opt_runsrf=5 due to runtime/restart bug")
    if science.gwd_opt == 1 and science.gwd_if_nonhyd:
        raise ValueError("WRF v4.8 requires gwd_if_nonhyd=False when gwd_opt=1")
    if science.shinhong_scu_mixing and science.shinhong_nonlocal_flux:
        raise ValueError("WRF v4.8 forbids shinhong_scu_mixing=True with shinhong_nonlocal_flux")
    # https://github.com/wrf-model/WRF/blob/v4.8.0/share/module_check_a_mundo.F#L319-L379
    if science.km_opt == 5 and (
        science.diff_opt != 2
        or science.bl_pbl_physics != 0
        or science.sf_sfclay_physics not in (0, 1, 5, 91)
        or science.cu_physics == 11  # Same source, scale-aware KF guard.
    ):
        raise ValueError(
            "km_opt=5 requires diff_opt=2, bl_pbl_physics=0, "
            "sf_sfclay_physics in (0, 1, 5, 91), and cu_physics other than 11"
        )
    # https://github.com/wrf-model/WRF/blob/v4.8.0/share/module_check_a_mundo.F#L2114-L2128
    if (science.bl_pbl_physics == 10) != (science.sf_sfclay_physics == 10):
        raise ValueError("TEMF requires both bl_pbl_physics=10 and sf_sfclay_physics=10")


def _validate_science_profile(science: ScienceProfile, domain: DomainProfile) -> None:
    """Enforce domain sizing policy and WRF v4.8 physics, CFL, AFWA, and sponge invariants."""
    validate_domain_profile(domain)
    if (science.wrf_version, science.wps_version) != ("4.8.0", "4.7.0"):
        raise ValueError("Supported namelist versions are WRF 4.8.0 and WPS 4.7.0")
    _validate_physics_compatibility(science)
    if science.use_theta_m not in (0, 1) or not (250.0 <= science.base_temp_k <= 320.0):
        raise ValueError("Invalid use_theta_m or base_temp_k")
    if science.hybrid_opt != 2:
        raise ValueError("The vertical preflight requires hybrid_opt=2")
    if science.afwa_diag_opt != 1 or science.afwa_buoy_opt != 1:
        raise ValueError("afwa_diag_opt=1 and afwa_buoy_opt=1 are required")
    if not (science.bucket_mm > 0 and science.history_interval_min > 0):
        raise ValueError("bucket_mm and history_interval_min must be positive")
    if 60 % science.history_interval_min:
        raise ValueError("history_interval_min must divide the hourly publication cadence")
    _validate_dynamics(science, domain)


def _physics_and_dynamics_sections(science: ScienceProfile) -> NamelistSections:
    return {
        "physics": {
            "mp_physics": science.mp_physics,
            "cu_physics": science.cu_physics,
            "ra_lw_physics": science.ra_lw_physics,
            "ra_sw_physics": science.ra_sw_physics,
            "radt": science.radt_min,
            "slope_rad": science.slope_rad,
            "topo_shading": science.topo_shading,
            "shadlen": science.shadlen_m,
            "sf_sfclay_physics": science.sf_sfclay_physics,
            "sf_surface_physics": science.sf_surface_physics,
            "bl_pbl_physics": science.bl_pbl_physics,
            "topo_wind": science.topo_wind,
            "ysu_topdown_pblmix": science.ysu_topdown_pblmix,
            "shinhong_scu_mixing": science.shinhong_scu_mixing,
            "shinhong_nonlocal_flux": science.shinhong_nonlocal_flux,
            "bucket_mm": science.bucket_mm,
        },
        "noah_mp": {"opt_runsrf": science.opt_runsrf, "opt_runsub": science.opt_runsub},
        "dynamics": {
            "use_theta_m": science.use_theta_m,
            "base_temp": science.base_temp_k,
            "hybrid_opt": science.hybrid_opt,
            "etac": science.etac,
            "diff_opt": science.diff_opt,
            "km_opt": science.km_opt,
            "diff_6th_opt": science.diff_6th_opt,
            "diff_6th_factor": science.diff_6th_factor,
            "epssm": science.epssm,
            "w_damping": science.w_damping,
            "damp_opt": science.damp_opt,
            "zdamp": science.zdamp_m,
            "dampcoef": science.dampcoef,
            "gwd_opt": science.gwd_opt,
            "gwd_if_nonhyd": science.gwd_if_nonhyd,
        },
        "afwa": {
            "afwa_diag_opt": science.afwa_diag_opt,
            "afwa_buoy_opt": science.afwa_buoy_opt,
        },
    }


def compile_wrf_namelist_sections(
    domain: DomainProfile,
    science: ScienceProfile,
    forcing: ForcingProfile,
    *,
    metgrid_levels: int | None = None,
    metgrid_soil_levels: int | None = None,
) -> NamelistSections:
    """Compile validated domain, science, and forcing profiles into WRF namelist sections."""
    _validate_science_profile(science, domain)
    mg_levels = forcing.metgrid_levels if metgrid_levels is None else metgrid_levels
    soil_levels = (
        forcing.metgrid_soil_levels if metgrid_soil_levels is None else metgrid_soil_levels
    )
    if min(mg_levels, soil_levels) < 2 or forcing.interval_seconds < 1:
        raise ValueError("WRF requires at least two air and soil levels and a positive interval")
    return {
        "time_control": {
            "interval_seconds": forcing.interval_seconds,
            "history_interval": science.history_interval_min,
            "frames_per_outfile": 1,
        },
        "domains": {
            "time_step": science.time_step_s,
            "max_dom": 1,
            "e_we": domain.e_we,
            "e_sn": domain.e_sn,
            "e_vert": domain.e_vert,
            "num_metgrid_levels": mg_levels,
            "num_metgrid_soil_levels": soil_levels,
            # WRF real can interpolate specific humidity via its mixing-ratio conversion.
            # https://github.com/wrf-model/WRF/blob/v4.8.0/dyn_em/module_initialize_real.F
            **({"use_sh_qv": True} if forcing.use_sh_qv else {}),
            "dx": domain.dx_m,
            "dy": domain.dy_m,
            "grid_id": 1,
            "parent_id": 0,
            "i_parent_start": 1,
            "j_parent_start": 1,
            "parent_grid_ratio": 1,
            "parent_time_step_ratio": 1,
            "feedback": 0,
            "smooth_option": 0,
            "p_top_requested": domain.p_top_pa,
            "auto_levels_opt": domain.auto_levels_opt,
            "dzbot": domain.dzbot_m,
            "dzstretch_s": domain.dzstretch_s,
            "dzstretch_u": domain.dzstretch_u,
            "max_dz": domain.max_dz_m,
        },
        **_physics_and_dynamics_sections(science),
        "bdy_control": {
            "spec_bdy_width": science.spec_bdy_width,
            "spec_zone": science.spec_zone,
            "relax_zone": science.relax_zone,
        },
        # MPI WRF requires this group even when no I/O ranks are reserved.
        # https://github.com/wrf-model/WRF/blob/v4.8.0/frame/module_io_quilt_old.F#L2474-L2480
        "namelist_quilt": {"nio_tasks_per_group": 0, "nio_groups": 1},
    }


def _build_geog_data_res(domain: DomainProfile) -> str:
    # GEOGRID tries resolution names in this order for each field in GEOGRID.TBL.
    # https://www2.mmm.ucar.edu/wrf/site/documentation/users_guide/wps.html
    custom = dict.fromkeys(
        src for src in (domain.landuse_source, domain.dem_source) if src != "default"
    )
    return "+".join((*custom, "default"))


def compile_geogrid_namelist_sections(domain: DomainProfile) -> NamelistSections:
    """Compile static geography sections for a validated domain."""
    return {
        "share": {
            "wrf_core": "ARW",
            "max_dom": 1,
            "io_form_geogrid": 2,
        },
        "geogrid": {
            "parent_id": 1,
            "parent_grid_ratio": 1,
            "i_parent_start": 1,
            "j_parent_start": 1,
            "e_we": domain.e_we,
            "e_sn": domain.e_sn,
            "geog_data_res": _build_geog_data_res(domain),
            "geog_data_path": domain.geog_data_path,
            "dx": domain.dx_m,
            "dy": domain.dy_m,
            "map_proj": "lambert",
            "ref_lat": domain.center_lat_deg,
            "ref_lon": domain.center_lon_deg,
            "truelat1": domain.std_parallel_1_deg,
            "truelat2": domain.std_parallel_2_deg,
            "stand_lon": domain.central_meridian_deg,
        },
    }


def _forcing_bracket_duration_s(window: RunLock | CycleWindow, interval_s: int) -> int:
    if interval_s <= 0 or interval_s % 3600:
        raise ValueError("Forcing interval must be a positive whole-hour multiple")
    origin = parse_iso_utc(window.publish_origin_utc)
    init = parse_iso_utc(window.wrf_init_utc)
    total_s = int((origin - init).total_seconds()) + max(window.lead_hours) * 3600
    return ((total_s + interval_s - 1) // interval_s) * interval_s


def required_forcing_forecast_hours(
    lock: RunLock | CycleWindow, source: ForcingProfile
) -> tuple[int, ...]:
    """Return source-native forecast hours, including the final WRF boundary bracket."""
    step_s = source.interval_seconds
    duration_s = _forcing_bracket_duration_s(lock, step_s)
    cycle = parse_iso_utc(lock.forcing_cycle_utc)
    init = parse_iso_utc(lock.wrf_init_utc)
    offset_s = (init - cycle).total_seconds()
    if offset_s < 0 or offset_s % step_s:
        raise ValueError("WRF init must align to a source-native forcing time")
    return tuple(
        range(int(offset_s) // 3600, (int(offset_s) + duration_s) // 3600 + 1, step_s // 3600)
    )


def compile_wps_namelist_sections(
    domain: DomainProfile, science: ScienceProfile, lock: RunLock, forcing: ForcingProfile
) -> NamelistSections:
    """Compile WPS sections with boundary inputs through the final bracketing time."""
    _validate_science_profile(science, domain)
    start_dt = parse_iso_utc(lock.wrf_init_utc)
    interval_s = forcing.interval_seconds
    end_dt = start_dt + timedelta(seconds=_forcing_bracket_duration_s(lock, interval_s))
    fmt = "%Y-%m-%d_%H:%M:%S"
    out = compile_geogrid_namelist_sections(domain)
    out["share"].update(
        start_date=start_dt.strftime(fmt),
        end_date=end_dt.strftime(fmt),
        interval_seconds=interval_s,
    )
    out["ungrib"] = {"out_format": "WPS", "prefix": "FILE"}
    out["metgrid"] = {"fg_name": "FILE", "io_form_metgrid": 2}
    return out


def compile_wrf_run_namelist(
    domain: DomainProfile,
    science: ScienceProfile,
    lock: RunLock,
    forcing: ForcingProfile,
    *,
    metgrid_levels: int | None = None,
    metgrid_soil_levels: int | None = None,
    for_real: bool = False,
) -> NamelistSections:
    """Attach RunLock UTC calendar and history_begin_h onto WRF namelist.input sections."""
    base = compile_wrf_namelist_sections(
        domain,
        science,
        forcing,
        metgrid_levels=metgrid_levels,
        metgrid_soil_levels=metgrid_soil_levels,
    )
    start_dt = parse_iso_utc(lock.wrf_init_utc)
    total_hours = lock.spinup_hours + max(lock.lead_hours)
    # real.exe reads end_* rather than run_hours to determine how many met_em files to open.
    # https://github.com/wrf-model/WRF/blob/v4.8.0/run/README.namelist
    end_s = (
        _forcing_bracket_duration_s(lock, forcing.interval_seconds)
        if for_real
        else (total_hours * 3600)
    )
    end_dt = start_dt + timedelta(seconds=end_s)
    base["time_control"] = {
        "run_days": 0,
        "run_hours": total_hours,
        "run_minutes": 0,
        "run_seconds": 0,
        "start_year": start_dt.year,
        "start_month": start_dt.month,
        "start_day": start_dt.day,
        "start_hour": start_dt.hour,
        "end_year": end_dt.year,
        "end_month": end_dt.month,
        "end_day": end_dt.day,
        "end_hour": end_dt.hour,
        "history_begin_h": lock.spinup_hours,
        "io_form_history": 2,
        "io_form_restart": 2,
        "io_form_input": 2,
        "io_form_boundary": 2,
        **base["time_control"],
    }
    base["bdy_control"]["specified"] = True
    return base


def _format_fortran_literal(val: NamelistScalar) -> str:
    if isinstance(val, bool):
        return ".true." if val else ".false."
    if isinstance(val, str):
        escaped = val.replace("'", "''")
        return f"'{escaped}'"
    return str(val)


def render_fortran_namelist(sections: NamelistSections) -> str:
    """Render namelist dictionary sections into canonical Fortran `&section ... /` text."""
    lines: list[str] = []
    for sec_name, mapping in sections.items():
        lines.append(f"&{sec_name}")
        for key, val in mapping.items():
            lines.append(f" {key} = {_format_fortran_literal(val)},")
        lines.extend((" /", ""))
    return "\n".join(lines)
