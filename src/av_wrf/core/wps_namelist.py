"""Compile WPS v4.7 geogrid/metgrid/ungrib namelists including 2-domain buffers."""

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
from av_wrf.core.grid import validate_domain_profile
from av_wrf.core.namelist import NamelistSections, forcing_bracket_duration_s
from av_wrf.core.nesting import nesting_active, parent_domain_shape
from av_wrf.core.physics_namelist import validate_science_profile


def _build_geog_data_res(domain: DomainProfile) -> str:
    # GEOGRID tries resolution names in this order for each field in GEOGRID.TBL.
    # https://www2.mmm.ucar.edu/wrf/site/documentation/users_guide/wps.html
    # `default` alone resolves lai_modis_10m/varsso_10m (~18.5 km) even when 30s tiles exist.
    tokens = [
        src
        for src in (
            domain.landuse_source,
            domain.dem_source,
            domain.lai_source,
            domain.varsso_source,
        )
        if src != "default"
    ]
    # dict preserves first-seen order while dropping duplicate tokens (e.g. two `30s`).
    return "+".join((*dict.fromkeys(tokens), "default"))


def compile_geogrid_namelist_sections(
    domain: DomainProfile, *, nested: bool = False
) -> NamelistSections:
    """Compile static geography sections for one domain or a concentric pair."""
    validate_domain_profile(domain)
    e_we, e_sn = domain.e_we, domain.e_sn
    parent_we, parent_sn = parent_domain_shape(domain) if nested else (e_we, e_sn)
    ratio = domain.parent_grid_ratio if nested else 1
    row = (1, domain.parent_halo_cells + 1) if nested else 1
    resolution = _build_geog_data_res(domain)
    return {
        "share": {
            "wrf_core": "ARW",
            "max_dom": 2 if nested else 1,
            "io_form_geogrid": 2,
        },
        "geogrid": {
            "parent_id": (1, 1) if nested else 1,
            "parent_grid_ratio": (1, ratio) if nested else 1,
            "i_parent_start": row,
            "j_parent_start": row,
            "e_we": (parent_we, e_we) if nested else e_we,
            "e_sn": (parent_sn, e_sn) if nested else e_sn,
            "geog_data_res": (resolution, resolution) if nested else resolution,
            "geog_data_path": domain.geog_data_path,
            # WPS `&geogrid` dx/dy are the coarse d01 spacing; d02 derives dx / ratio.
            "dx": domain.dx_m * ratio,
            "dy": domain.dy_m * ratio,
            "map_proj": "lambert",
            "ref_lat": domain.center_lat_deg,
            "ref_lon": domain.center_lon_deg,
            "truelat1": domain.std_parallel_1_deg,
            "truelat2": domain.std_parallel_2_deg,
            "stand_lon": domain.central_meridian_deg,
        },
    }


def required_forcing_forecast_hours(
    lock: RunLock | CycleWindow, source: ForcingProfile
) -> tuple[int, ...]:
    """Return source-native forecast hours, including the final WRF boundary bracket."""
    step_s = source.interval_seconds
    duration_s = forcing_bracket_duration_s(lock, step_s)
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
    validate_science_profile(science, domain)
    nested = nesting_active(domain, forcing.grid_spacing_deg)
    start_dt = parse_iso_utc(lock.wrf_init_utc)
    interval_s = forcing.interval_seconds
    end_dt = start_dt + timedelta(seconds=forcing_bracket_duration_s(lock, interval_s))
    fmt = "%Y-%m-%d_%H:%M:%S"
    out = compile_geogrid_namelist_sections(domain, nested=nested)
    start_date, end_date = start_dt.strftime(fmt), end_dt.strftime(fmt)
    out["share"].update(
        start_date=(start_date, start_date) if nested else start_date,
        # A one-way nest reads lateral conditions from its parent after initialization.
        end_date=(end_date, start_date) if nested else end_date,
        interval_seconds=interval_s,
    )
    out["ungrib"] = {"out_format": "WPS", "prefix": "FILE"}
    out["metgrid"] = {"fg_name": "FILE", "io_form_metgrid": 2}
    return out


__all__ = [
    "compile_geogrid_namelist_sections",
    "compile_wps_namelist_sections",
    "required_forcing_forecast_hours",
]
