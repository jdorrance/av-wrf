"""Compile WRF v4.8 Fortran namelists from validated domain, science, and forcing profiles."""

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
from av_wrf.core.nesting import nesting_active, parent_domain_shape
from av_wrf.core.physics_namelist import (
    NamelistSections,
    NamelistValue,
    adaptive_domains,
    per_domain,
    physics_and_dynamics_sections,
    validate_science_profile,
)

MYNN_CLOUD_FIELD = "CLDFRA_BL"
MYNN_HISTORY_FILE = "mynn-history.txt"
MYNN_HISTORY_TEXT = f"+:h:0:{MYNN_CLOUD_FIELD}\n"


def _wrf_domains(
    domain: DomainProfile,
    science: ScienceProfile,
    forcing: ForcingProfile,
    *,
    metgrid_levels: int | None,
    metgrid_soil_levels: int | None,
    nested: bool,
) -> dict[str, NamelistValue]:
    max_dom, ratio = (2, domain.parent_grid_ratio) if nested else (1, 1)
    mg_levels = forcing.metgrid_levels if metgrid_levels is None else metgrid_levels
    soil_levels = (
        forcing.metgrid_soil_levels if metgrid_soil_levels is None else metgrid_soil_levels
    )
    if min(mg_levels, soil_levels) < 2 or forcing.interval_seconds < 1:
        raise ValueError("WRF requires at least two air and soil levels and a positive interval")
    e_we, e_sn = domain.e_we, domain.e_sn
    parent_we, parent_sn = parent_domain_shape(domain) if nested else (e_we, e_sn)
    dx, dy = domain.dx_m, domain.dy_m
    start = (1, domain.parent_halo_cells + 1) if nested else 1
    domains: dict[str, NamelistValue] = {
        "time_step": science.time_step_s * ratio,
        "max_dom": max_dom,
        "e_we": (parent_we, e_we) if nested else e_we,
        "e_sn": (parent_sn, e_sn) if nested else e_sn,
        "e_vert": per_domain(domain.e_vert, max_dom),
        "num_metgrid_levels": mg_levels,
        "num_metgrid_soil_levels": soil_levels,
        # WRF real interpolates specific humidity via its mixing-ratio conversion.
        **({"use_sh_qv": True} if forcing.use_sh_qv else {}),
        "dx": (dx * ratio, dx) if nested else dx,
        "dy": (dy * ratio, dy) if nested else dy,
        "grid_id": (1, 2) if nested else 1,
        "parent_id": (1, 1) if nested else 0,
        "i_parent_start": start,
        "j_parent_start": start,
        "parent_grid_ratio": (1, ratio) if nested else 1,
        "parent_time_step_ratio": (1, ratio) if nested else 1,
        "feedback": 0,
        "smooth_option": 0,
        "p_top_requested": domain.p_top_pa,
        "auto_levels_opt": domain.auto_levels_opt,
        "dzbot": domain.dzbot_m,
        "dzstretch_s": domain.dzstretch_s,
        "dzstretch_u": domain.dzstretch_u,
        "max_dz": domain.max_dz_m,
    }
    if science.use_adaptive_time_step:
        domains.update(adaptive_domains(science, ratio))
    return domains


def compile_wrf_namelist_sections(
    domain: DomainProfile,
    science: ScienceProfile,
    forcing: ForcingProfile,
    *,
    metgrid_levels: int | None = None,
    metgrid_soil_levels: int | None = None,
    nested: bool = False,
) -> NamelistSections:
    """Compile validated profiles into WRF namelist sections (1 or 2 concentric domains)."""
    validate_science_profile(science, domain)
    max_dom = 2 if nested else 1
    return {
        "time_control": {
            "interval_seconds": forcing.interval_seconds,
            "history_interval": per_domain(science.history_interval_min, max_dom),
            "frames_per_outfile": per_domain(1, max_dom),
            "input_from_file": per_domain(True, max_dom),
        },
        "domains": _wrf_domains(
            domain,
            science,
            forcing,
            metgrid_levels=metgrid_levels,
            metgrid_soil_levels=metgrid_soil_levels,
            nested=nested,
        ),
        **physics_and_dynamics_sections(science, max_dom),
        "bdy_control": {
            # Parent d01 is driven by met_em; only the nested child is relaxed from d01.
            "specified": (True, False) if nested else True,
            "nested": (False, True) if nested else False,
            "spec_bdy_width": science.spec_bdy_width,
            "spec_zone": science.spec_zone,
            "relax_zone": science.relax_zone,
        },
        # MPI WRF requires this group even when no I/O ranks are reserved.
        # https://github.com/wrf-model/WRF/blob/v4.8.0/frame/module_io_quilt_old.F#L2474-L2480
        "namelist_quilt": {"nio_tasks_per_group": 0, "nio_groups": 1},
    }


def forcing_bracket_duration_s(window: RunLock | CycleWindow, interval_s: int) -> int:
    """Return elapsed seconds from WRF initialization through the final source bracket."""
    if interval_s <= 0 or interval_s % 3600:
        raise ValueError("Forcing interval must be a positive whole-hour multiple")
    origin = parse_iso_utc(window.publish_origin_utc)
    init = parse_iso_utc(window.wrf_init_utc)
    total_s = int((origin - init).total_seconds()) + max(window.lead_hours) * 3600
    return ((total_s + interval_s - 1) // interval_s) * interval_s


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
    nested = nesting_active(domain, forcing.grid_spacing_deg)
    base = compile_wrf_namelist_sections(
        domain,
        science,
        forcing,
        metgrid_levels=metgrid_levels,
        metgrid_soil_levels=metgrid_soil_levels,
        nested=nested,
    )
    start_dt = parse_iso_utc(lock.wrf_init_utc)
    total_hours = lock.spinup_hours + max(lock.lead_hours)
    # real.exe reads end_* rather than run_hours to determine how many met_em files to open.
    # https://github.com/wrf-model/WRF/blob/v4.8.0/run/README.namelist
    end_s = (
        forcing_bracket_duration_s(lock, forcing.interval_seconds)
        if for_real
        else (total_hours * 3600)
    )
    end_dt = start_dt + timedelta(seconds=end_s)
    # WRF adapts the parent clock modulo its history cadence. Delay writes independently.
    # https://github.com/wrf-model/WRF/blob/v4.8.0/dyn_em/adapt_timestep_em.F#L321-L355
    begin = (end_s // 3600 + 1, lock.spinup_hours) if nested else lock.spinup_hours
    max_dom = 2 if nested else 1
    base["time_control"] = {
        "run_days": 0,
        "run_hours": total_hours,
        "run_minutes": 0,
        "run_seconds": 0,
        "start_year": per_domain(start_dt.year, max_dom),
        "start_month": per_domain(start_dt.month, max_dom),
        "start_day": per_domain(start_dt.day, max_dom),
        "start_hour": per_domain(start_dt.hour, max_dom),
        "end_year": per_domain(end_dt.year, max_dom),
        "end_month": per_domain(end_dt.month, max_dom),
        "end_day": per_domain(end_dt.day, max_dom),
        "end_hour": per_domain(end_dt.hour, max_dom),
        "history_begin_h": begin,
        "io_form_history": 2,
        "io_form_restart": 2,
        "io_form_input": 2,
        "io_form_boundary": 2,
        **base["time_control"],
    }
    if not for_real and science.bl_pbl_physics == 5 and science.icloud_bl > 0:
        # The native Registry includes these MYNN fields only on the restart stream.
        base["time_control"]["iofields_filename"] = per_domain(MYNN_HISTORY_FILE, max_dom)
        base["time_control"]["ignore_iofields_warning"] = False
    return base


def _format_fortran_literal(val: NamelistValue) -> str:
    if isinstance(val, tuple):
        return ", ".join(_format_fortran_literal(item) for item in val)
    if isinstance(val, bool):
        return ".true." if val else ".false."
    if isinstance(val, str):
        return "'" + val.replace("'", "''") + "'"
    return str(val)


def render_fortran_namelist(sections: NamelistSections) -> str:
    """Render namelist dictionary sections into canonical Fortran `&section ... /` text."""
    lines: list[str] = []
    for sec_name, mapping in sections.items():
        lines.append(f"&{sec_name}")
        lines.extend(f" {key} = {_format_fortran_literal(val)}," for key, val in mapping.items())
        lines.extend((" /", ""))
    return "\n".join(lines)


__all__ = [
    "MYNN_CLOUD_FIELD",
    "MYNN_HISTORY_FILE",
    "MYNN_HISTORY_TEXT",
    "NamelistSections",
    "NamelistValue",
    "compile_wrf_namelist_sections",
    "compile_wrf_run_namelist",
    "forcing_bracket_duration_s",
    "render_fortran_namelist",
    "validate_science_profile",
]
