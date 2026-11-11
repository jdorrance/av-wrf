"""Pure WRF v4.8 / WPS v4.7 namelist validation, compilation, and Fortran rendering.

Assumptions & Solver Physics Guardrails:
1. Map-projected RK3 CFL constraint (`Skamarock et al. 2019 NCAR/TN-556+STR Sec. 3.3.4`):
   `time_step_s <= 6 * min(dx_m, dy_m) / (1000 * m_max)` evaluated across the computational
   domain's north/center/south latitudes so map-factor distortion (`m > 1`) cannot violate CFL.
2. Topographic radiation (`slope_rad=1, topo_shading=1, shadlen=25000`): 3 km slope aspect and
   25 km cast shadows enter Noah-MP `ACHFX`/`ACLHF`/`T2` directly; downstream consumer must NOT
   apply an additional slope-irradiance multiplier on top of WRF surface heat fluxes.
3. Surface-layer `T2` coupling (`sf_sfclay_physics=1`): Revised MM5 diagnoses `T2` along the
   Monin-Obukhov log profile between `TSK` and `temp_k[0]`, providing resolved superadiabatic
   surface excess so consumer parcel ascent needs `parcelSurfaceDeltaTC = 0 K`.
4. WRF v4.8 Registry & Known-Problem guards: emits `nwp_diagnostics` under `&time_control`,
   renders `shinhong_scu_mixing` as a Fortran logical (`.false.`), enables `afwa_buoy_opt=1`
   so `AFWA_CAPE` and `AFWA_CAPE_MU` execute under Thompson `mp_physics=8`, and rejects
   `opt_runsrf=5`, `gwd_if_nonhyd=True` with `gwd_opt=1`, and `shinhong_scu_mixing=True`
   with `shinhong_nonlocal_flux=True`.

References:
- WRF v4.8 Registry: https://github.com/wrf-model/WRF/blob/v4.8.0/Registry/Registry.EM_COMMON
- WRF AFWA Registry: https://github.com/wrf-model/WRF/blob/v4.8.0/Registry/registry.afwa
- WRF v4.8 Known Problems: https://github.com/wrf-model/WRF/wiki/WRF-V4.8.0-Known-Problems
- WRF v4 Technical Note (Sec. 3.3.4):
  https://www2.mmm.ucar.edu/wrf/users/docs/technote/v4_technote.pdf
"""

from __future__ import annotations

import math
from datetime import timedelta

from av_wrf.contracts import (
    DomainProfile,
    ForcingProfile,
    RunLock,
    ScienceProfile,
    parse_iso_utc,
)
from av_wrf.core.grid import lambert_map_scale, validate_domain_profile

NamelistScalar = int | float | str | bool
NamelistSections = dict[str, dict[str, NamelistScalar]]

NON_NAMELIST_METADATA_FIELDS: frozenset[str] = frozenset(
    {"profile_id", "wrf_version", "wps_version"}
)


def _max_domain_map_scale(domain: DomainProfile) -> float:
    half_span_deg = math.degrees(
        0.5 * max(1, domain.e_sn - 1) * abs(domain.dy_m) / domain.earth_radius_m
    )
    sample_lats = (
        max(-89.0, domain.center_lat_deg - half_span_deg),
        domain.center_lat_deg,
        min(89.0, domain.center_lat_deg + half_span_deg),
    )
    return max(
        lambert_map_scale(
            lat,
            std_parallel_1_deg=domain.std_parallel_1_deg,
            std_parallel_2_deg=domain.std_parallel_2_deg,
            origin_lat_deg=domain.center_lat_deg,
            radius_m=domain.earth_radius_m,
        )
        for lat in sample_lats
    )


def validate_science_profile(science: ScienceProfile, domain: DomainProfile) -> None:
    """Enforce domain sizing policy and WRF v4.8 physics, CFL, AFWA, and sponge invariants."""
    validate_domain_profile(domain)
    if science.sf_surface_physics == 4 and science.opt_runsrf == 5:
        raise ValueError("WRF v4.8 Noah-MP forbids opt_runsrf=5 due to runtime/restart bug")
    if science.gwd_opt == 1 and science.gwd_if_nonhyd:
        raise ValueError("WRF v4.8 requires gwd_if_nonhyd=False when gwd_opt=1")
    if science.shinhong_scu_mixing and science.shinhong_nonlocal_flux:
        raise ValueError("WRF v4.8 forbids shinhong_scu_mixing=True with shinhong_nonlocal_flux")
    if science.use_theta_m not in (0, 1) or not (250.0 <= science.base_temp_k <= 320.0):
        raise ValueError("Invalid use_theta_m or base_temp_k")
    if (
        science.afwa_diag_opt != 1
        or science.afwa_severe_opt != 1
        or science.afwa_buoy_opt != 1
        or science.nwp_diagnostics != 1
    ):
        raise ValueError("nwp_diagnostics=1 and afwa_{diag,severe,buoy}_opt=1 are required")
    if science.bucket_mm <= 0.0 or science.bucket_j <= 0.0 or science.history_interval_min < 1:
        raise ValueError("Accumulator buckets and history_interval_min must be positive")
    m_max = _max_domain_map_scale(domain)
    min_phys_dx_km = (min(abs(domain.dx_m), abs(domain.dy_m)) / m_max) / 1000.0
    max_dt_s = 6.0 * min_phys_dx_km
    if science.time_step_s <= 0 or science.time_step_s > max_dt_s:
        raise ValueError(
            f"time_step_s ({science.time_step_s}) exceeds 6*dx_phys ({max_dt_s:.2f}s, "
            f"m_max={m_max:.4f})"
        )
    if science.spec_bdy_width != science.spec_zone + science.relax_zone:
        raise ValueError("spec_bdy_width must equal spec_zone + relax_zone")
    if domain.bdy_trim_cells <= science.spec_bdy_width:
        raise ValueError("bdy_trim_cells must exceed spec_bdy_width")


def _physics_and_dynamics_sections(
    science: ScienceProfile,
) -> dict[str, dict[str, int | float | bool]]:
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
            "bucket_J": science.bucket_j,
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
            "afwa_severe_opt": science.afwa_severe_opt,
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
) -> dict[str, dict[str, int | float | bool]]:
    """Compile validated domain, science, and forcing profiles into WRF namelist sections."""
    validate_science_profile(science, domain)
    mg_levels = forcing.pressure_level_count if metgrid_levels is None else metgrid_levels
    soil_levels = (
        len(forcing.soil_layer_centers_cm) if metgrid_soil_levels is None else metgrid_soil_levels
    )
    if mg_levels < 2 or soil_levels < 1:
        raise ValueError("num_metgrid_levels and num_metgrid_soil_levels must be positive")
    return {
        "time_control": {
            "interval_seconds": science.interval_seconds,
            "history_interval": science.history_interval_min,
            "frames_per_outfile": 1,
            "nwp_diagnostics": science.nwp_diagnostics,
        },
        "domains": {
            "time_step": science.time_step_s,
            "max_dom": 1,
            "e_we": domain.e_we,
            "e_sn": domain.e_sn,
            "e_vert": domain.e_vert,
            "num_metgrid_levels": mg_levels,
            "num_metgrid_soil_levels": soil_levels,
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
    }


def compile_wps_namelist_sections(
    domain: DomainProfile,
    science: ScienceProfile,
    lock: RunLock,
    *,
    interval_seconds: int | None = None,
) -> NamelistSections:
    """Compile WPS v4.7 namelist.wps sections bracketing RunLock integration window."""
    start_dt = parse_iso_utc(lock.wrf_init_utc)
    step_s = science.interval_seconds if interval_seconds is None else interval_seconds
    if step_s < 1:
        raise ValueError("WPS interval_seconds must be positive")
    raw_s = (lock.spinup_hours + max(lock.lead_hours)) * 3600
    bracket_s = ((raw_s + step_s - 1) // step_s) * step_s
    end_dt = start_dt + timedelta(seconds=bracket_s)
    fmt = "%Y-%m-%d_%H:%M:%S"
    geog_res = (
        domain.dem_source
        if domain.dem_source == domain.landuse_source
        else f"{domain.dem_source}+{domain.landuse_source}"
    )
    return {
        "share": {
            "wrf_core": "ARW",
            "max_dom": 1,
            "start_date": start_dt.strftime(fmt),
            "end_date": end_dt.strftime(fmt),
            "interval_seconds": step_s,
            "io_form_geogrid": 2,
        },
        "geogrid": {
            "parent_id": 1,
            "parent_grid_ratio": 1,
            "i_parent_start": 1,
            "j_parent_start": 1,
            "e_we": domain.e_we,
            "e_sn": domain.e_sn,
            "geog_data_res": geog_res,
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
        "ungrib": {"out_format": "WPS", "prefix": "FILE"},
        "metgrid": {"fg_name": "FILE", "io_form_metgrid": 2},
    }


def compile_wrf_run_namelist(
    domain: DomainProfile,
    science: ScienceProfile,
    lock: RunLock,
    forcing: ForcingProfile,
    *,
    metgrid_levels: int | None = None,
    metgrid_soil_levels: int | None = None,
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
    end_dt = start_dt + timedelta(hours=total_hours)
    tc: dict[str, NamelistScalar] = {
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
    out: NamelistSections = {"time_control": tc}
    for sec_name, sec_vals in base.items():
        if sec_name != "time_control":
            out[sec_name] = dict(sec_vals)
    out["bdy_control"]["specified"] = True
    return out


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
        lines.append(" /")
        lines.append("")
    return "\n".join(lines)
