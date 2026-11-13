"""Strict declarative dictionary-to-dataclass parsers with key, type, and range checks."""

from __future__ import annotations

import dataclasses
import math
import re
from collections.abc import Mapping

from av_wrf.contracts.config import (
    DomainProfile,
    ForcingProfile,
    OutputProfile,
    ProductSpec,
    ScienceProfile,
)
from av_wrf.contracts.zarr_schema import validate_zarr_output


def _check_keys(
    name: str,
    expected: frozenset[str],
    raw: Mapping[str, object],
    optional: frozenset[str] = frozenset(),
) -> None:
    unknown = sorted(set(raw.keys()) - expected)
    missing = sorted(expected - optional - set(raw.keys()))
    if unknown:
        raise ValueError(f"{name} received unknown keys: {', '.join(unknown)}")
    if missing:
        raise ValueError(f"{name} missing required keys: {', '.join(missing)}")


def _req_str(raw: Mapping[str, object], key: str) -> str:
    val = raw[key]
    if not isinstance(val, str) or not val.strip():
        raise TypeError(f"Expected non-empty str for '{key}', got {type(val).__name__}")
    return val


def _req_bool(raw: Mapping[str, object], key: str) -> bool:
    val = raw[key]
    if not isinstance(val, bool):
        raise TypeError(f"Expected bool for '{key}', got {type(val).__name__}")
    return val


def _req_int(raw: Mapping[str, object], key: str) -> int:
    val = raw[key]
    if isinstance(val, bool) or not isinstance(val, int):
        raise TypeError(f"Expected int for '{key}', got {type(val).__name__}")
    return val


def _req_float(raw: Mapping[str, object], key: str) -> float:
    val = raw[key]
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise TypeError(f"Expected float for '{key}', got {type(val).__name__}")
    result = float(val)
    if not math.isfinite(result):
        raise ValueError(f"Expected finite float for '{key}'")
    return result


def _req_seq(raw: Mapping[str, object], key: str) -> list[object]:
    val = raw[key]
    if not isinstance(val, (list, tuple)) or not val:
        raise TypeError(f"Expected non-empty sequence for '{key}'")
    return list(val)


def _req_float_tuple(raw: Mapping[str, object], key: str) -> tuple[float, ...]:
    items = _req_seq(raw, key)
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in items):
        raise TypeError(f"Expected numeric sequence for '{key}'")
    values = tuple(float(x) for x in items if isinstance(x, (int, float)))
    if not all(map(math.isfinite, values)):
        raise ValueError(f"Expected finite numbers for '{key}'")
    return values


def _req_int_pair(raw: Mapping[str, object], key: str) -> tuple[int, int]:
    items = _req_seq(raw, key)
    if len(items) != 2 or any(isinstance(x, bool) or not isinstance(x, int) for x in items):
        raise TypeError(f"Expected 2-int sequence for '{key}'")
    a, b = items[0], items[1]
    if not isinstance(a, int) or not isinstance(b, int) or a < 1 or b < 1:
        raise ValueError(f"Chunk dimensions in '{key}' must be >= 1")
    return (a, b)


def _req_str_tuple(raw: Mapping[str, object], key: str) -> tuple[str, ...]:
    items = _req_seq(raw, key)
    if any(not isinstance(x, str) or not x.strip() for x in items):
        raise TypeError(f"Expected non-empty str sequence for '{key}'")
    return tuple(x for x in items if isinstance(x, str))


def _opt_str(raw: Mapping[str, object], key: str, default: str) -> str:
    return _req_str(raw, key) if key in raw else default


def _opt_int(raw: Mapping[str, object], key: str, default: int) -> int:
    return _req_int(raw, key) if key in raw else default


def _opt_float(raw: Mapping[str, object], key: str, default: float) -> float:
    return _req_float(raw, key) if key in raw else default


def _opt_bool(raw: Mapping[str, object], key: str, default: bool) -> bool:
    return _req_bool(raw, key) if key in raw else default


def _ocean_seeds(raw: Mapping[str, object]) -> tuple[tuple[float, float], ...]:
    key = "ocean_seeds_lat_lon_deg"
    items = raw.get(key, DomainProfile.ocean_seeds_lat_lon_deg)
    if not isinstance(items, (list, tuple)):
        raise TypeError(f"Expected sequence for '{key}'")
    seeds: list[tuple[float, float]] = []
    for item in items:
        pair = _req_float_tuple({key: item}, key)
        if len(pair) != 2 or not (-90.0 < pair[0] < 90.0 and -180.0 <= pair[1] <= 180.0):
            raise ValueError(f"Expected valid Lambert latitude/longitude pair for '{key}'")
        seed = pair[0], pair[1]
        if seed in seeds:
            raise ValueError(f"Duplicate latitude/longitude pair in '{key}'")
        seeds.append(seed)
    return tuple(seeds)


def parse_domain_profile(raw: Mapping[str, object]) -> DomainProfile:
    optional = frozenset(
        f.name for f in dataclasses.fields(DomainProfile) if f.default is not dataclasses.MISSING
    )
    _check_keys("DomainProfile", frozenset(DomainProfile.__dataclass_fields__), raw, optional)
    dom = DomainProfile(
        profile_id=_req_str(raw, "profile_id"),
        earth_radius_m=_req_float(raw, "earth_radius_m"),
        center_lat_deg=_req_float(raw, "center_lat_deg"),
        center_lon_deg=_req_float(raw, "center_lon_deg"),
        std_parallel_1_deg=_req_float(raw, "std_parallel_1_deg"),
        std_parallel_2_deg=_req_float(raw, "std_parallel_2_deg"),
        central_meridian_deg=_req_float(raw, "central_meridian_deg"),
        dx_m=_req_float(raw, "dx_m"),
        dy_m=_req_float(raw, "dy_m"),
        e_we=_req_int(raw, "e_we"),
        e_sn=_req_int(raw, "e_sn"),
        bdy_trim_cells=_req_int(raw, "bdy_trim_cells"),
        e_vert=_req_int(raw, "e_vert"),
        exported_levels_k=_req_int(raw, "exported_levels_k"),
        p_top_pa=_req_float(raw, "p_top_pa"),
        auto_levels_opt=_req_int(raw, "auto_levels_opt"),
        dzbot_m=_req_float(raw, "dzbot_m"),
        dzstretch_s=_req_float(raw, "dzstretch_s"),
        dzstretch_u=_req_float(raw, "dzstretch_u"),
        max_dz_m=_req_float(raw, "max_dz_m"),
        min_bl_levels_2km_agl=_req_int(raw, "min_bl_levels_2km_agl"),
        terrain_elevations_m=_req_float_tuple(raw, "terrain_elevations_m"),
        dem_source=_req_str(raw, "dem_source"),
        landuse_source=_req_str(raw, "landuse_source"),
        geog_data_path=_req_str(raw, "geog_data_path"),
        lai_source=_opt_str(raw, "lai_source", DomainProfile.lai_source),
        varsso_source=_opt_str(raw, "varsso_source", DomainProfile.varsso_source),
        parent_grid_ratio=_opt_int(raw, "parent_grid_ratio", DomainProfile.parent_grid_ratio),
        parent_halo_cells=_opt_int(raw, "parent_halo_cells", DomainProfile.parent_halo_cells),
        nest_trigger_ratio=_opt_float(raw, "nest_trigger_ratio", DomainProfile.nest_trigger_ratio),
        ocean_seeds_lat_lon_deg=_ocean_seeds(raw),
    )
    if dom.earth_radius_m <= 0.0 or dom.dx_m <= 0.0 or dom.dy_m <= 0.0 or dom.p_top_pa <= 0.0:
        raise ValueError("Domain radius, grid spacing, and p_top_pa must be positive")
    # Sub-sea basins (Caspian, Death Valley, Dead Sea) are valid; thermo handles them.
    if any(elev < -500.0 for elev in dom.terrain_elevations_m):
        raise ValueError("terrain_elevations_m entries must be >= -500.0 m")
    if dom.parent_grid_ratio not in (1, 2, 3) or dom.parent_halo_cells < 0:
        raise ValueError("parent_grid_ratio must be 1, 2 or 3 and parent_halo_cells >= 0")
    if dom.parent_grid_ratio > 1:
        if dom.parent_halo_cells < 1 or (dom.e_we - 1) % dom.parent_grid_ratio:
            raise ValueError("Nested domains require halo cells and divisible e_we/e_sn")
        if (dom.e_sn - 1) % dom.parent_grid_ratio:
            raise ValueError("Nested domains require halo cells and divisible e_we/e_sn")
    return dom


def _parse_science_core(raw: Mapping[str, object]) -> ScienceProfile:
    optional = frozenset(
        f.name for f in dataclasses.fields(ScienceProfile) if f.default is not dataclasses.MISSING
    )
    _check_keys("ScienceProfile", frozenset(ScienceProfile.__dataclass_fields__), raw, optional)
    return ScienceProfile(
        profile_id=_req_str(raw, "profile_id"),
        wrf_version=_req_str(raw, "wrf_version"),
        wps_version=_req_str(raw, "wps_version"),
        mp_physics=_req_int(raw, "mp_physics"),
        ra_lw_physics=_req_int(raw, "ra_lw_physics"),
        ra_sw_physics=_req_int(raw, "ra_sw_physics"),
        radt_min=_req_int(raw, "radt_min"),
        slope_rad=_req_int(raw, "slope_rad"),
        topo_shading=_req_int(raw, "topo_shading"),
        shadlen_m=_req_float(raw, "shadlen_m"),
        sf_surface_physics=_req_int(raw, "sf_surface_physics"),
        opt_runsrf=_req_int(raw, "opt_runsrf"),
        opt_runsub=_req_int(raw, "opt_runsub"),
        bl_pbl_physics=_req_int(raw, "bl_pbl_physics"),
        sf_sfclay_physics=_req_int(raw, "sf_sfclay_physics"),
        topo_wind=_req_int(raw, "topo_wind"),
        ysu_topdown_pblmix=_req_int(raw, "ysu_topdown_pblmix"),
        shinhong_scu_mixing=_req_bool(raw, "shinhong_scu_mixing"),
        shinhong_nonlocal_flux=_req_bool(raw, "shinhong_nonlocal_flux"),
        cu_physics=_req_int(raw, "cu_physics"),
        gwd_opt=_req_int(raw, "gwd_opt"),
        gwd_if_nonhyd=_req_bool(raw, "gwd_if_nonhyd"),
        use_theta_m=_req_int(raw, "use_theta_m"),
        base_temp_k=_req_float(raw, "base_temp_k"),
        hybrid_opt=_req_int(raw, "hybrid_opt"),
        etac=_req_float(raw, "etac"),
        diff_opt=_req_int(raw, "diff_opt"),
        km_opt=_req_int(raw, "km_opt"),
        diff_6th_opt=_req_int(raw, "diff_6th_opt"),
        diff_6th_factor=_req_float(raw, "diff_6th_factor"),
        epssm=_req_float(raw, "epssm"),
        w_damping=_req_int(raw, "w_damping"),
        damp_opt=_req_int(raw, "damp_opt"),
        zdamp_m=_req_float(raw, "zdamp_m"),
        dampcoef=_req_float(raw, "dampcoef"),
        time_step_s=_req_int(raw, "time_step_s"),
        spec_bdy_width=_req_int(raw, "spec_bdy_width"),
        spec_zone=_req_int(raw, "spec_zone"),
        relax_zone=_req_int(raw, "relax_zone"),
        afwa_diag_opt=_req_int(raw, "afwa_diag_opt"),
        afwa_buoy_opt=_req_int(raw, "afwa_buoy_opt"),
        history_interval_min=_req_int(raw, "history_interval_min"),
        bucket_mm=_req_float(raw, "bucket_mm"),
    )


def parse_science_profile(raw: Mapping[str, object]) -> ScienceProfile:
    core = _parse_science_core(raw)
    return dataclasses.replace(
        core,
        bl_mynn_edmf=_opt_int(raw, "bl_mynn_edmf", ScienceProfile.bl_mynn_edmf),
        icloud=_opt_int(raw, "icloud", ScienceProfile.icloud),
        icloud_bl=_opt_int(raw, "icloud_bl", ScienceProfile.icloud_bl),
        sf_lake_physics=_opt_int(raw, "sf_lake_physics", ScienceProfile.sf_lake_physics),
        use_lakedepth=_opt_int(raw, "use_lakedepth", ScienceProfile.use_lakedepth),
        lakedepth_default_m=_opt_float(
            raw, "lakedepth_default_m", ScienceProfile.lakedepth_default_m
        ),
        lake_min_elev_m=_opt_float(raw, "lake_min_elev_m", ScienceProfile.lake_min_elev_m),
        diff_6th_slopeopt=_opt_int(raw, "diff_6th_slopeopt", ScienceProfile.diff_6th_slopeopt),
        diff_6th_thresh=_opt_float(raw, "diff_6th_thresh", ScienceProfile.diff_6th_thresh),
        use_adaptive_time_step=_opt_bool(
            raw, "use_adaptive_time_step", ScienceProfile.use_adaptive_time_step
        ),
        target_cfl=_opt_float(raw, "target_cfl", ScienceProfile.target_cfl),
        target_hcfl=_opt_float(raw, "target_hcfl", ScienceProfile.target_hcfl),
        min_time_step_s=_opt_int(raw, "min_time_step_s", ScienceProfile.min_time_step_s),
        max_time_step_s=_opt_int(raw, "max_time_step_s", ScienceProfile.max_time_step_s),
    )


def parse_forcing_profile(raw: Mapping[str, object]) -> ForcingProfile:
    levels_key = "metgrid_levels" if "metgrid_levels" in raw else "pressure_level_count"
    soil_key = "metgrid_soil_levels" if "metgrid_soil_levels" in raw else "soil_layer_centers_cm"
    keys = frozenset(ForcingProfile.__dataclass_fields__) - {
        "metgrid_levels",
        "metgrid_soil_levels",
    }
    _check_keys("ForcingProfile", keys | {levels_key, soil_key}, raw, frozenset({"use_sh_qv"}))
    soil_levels = (
        _req_int(raw, soil_key)
        if soil_key == "metgrid_soil_levels"
        else len(_req_float_tuple(raw, soil_key))
    )
    prof = ForcingProfile(
        profile_id=_req_str(raw, "profile_id"),
        provider=_req_str(raw, "provider"),
        vtable_name=_req_str(raw, "vtable_name"),
        interval_seconds=_req_int(raw, "interval_seconds"),
        grid_spacing_deg=_req_float(raw, "grid_spacing_deg"),
        metgrid_levels=_req_int(raw, levels_key),
        metgrid_soil_levels=soil_levels,
        source_ready_lag_p95_hours=_req_float(raw, "source_ready_lag_p95_hours"),
        mirrors=_req_str_tuple(raw, "mirrors"),
        fail_closed=_req_bool(raw, "fail_closed"),
        use_sh_qv=_opt_bool(raw, "use_sh_qv", ForcingProfile.use_sh_qv),
    )
    if not re.fullmatch(r"Vtable\.[A-Za-z0-9][A-Za-z0-9_.-]*", prof.vtable_name):
        raise ValueError("ForcingProfile.vtable_name must be a single WPS Vtable filename")
    if prof.interval_seconds < 3600 or prof.interval_seconds % 3600 != 0:
        raise ValueError("ForcingProfile.interval_seconds must be a positive multiple of 3600")
    if prof.metgrid_levels < 10 or prof.metgrid_soil_levels < 2:
        raise ValueError("ForcingProfile requires at least 10 metgrid levels and two soil layers")
    if prof.source_ready_lag_p95_hours <= 0.0:
        raise ValueError("ForcingProfile requires positive source ready lag")
    return prof


def parse_output_profile(raw: Mapping[str, object]) -> OutputProfile:
    _check_keys("OutputProfile", frozenset(OutputProfile.__dataclass_fields__), raw)
    out = OutputProfile(
        profile_id=_req_str(raw, "profile_id"),
        schema_id=_req_str(raw, "schema_id"),
        map_end_hour=_req_int(raw, "map_end_hour"),
        hourly_through_hour=_req_int(raw, "hourly_through_hour"),
        tail_step_hours=_req_int(raw, "tail_step_hours"),
        shard_time_steps=_req_int(raw, "shard_time_steps"),
        inner_chunk_yx=_req_int_pair(raw, "inner_chunk_yx"),
        zstd_level=_req_int(raw, "zstd_level"),
    )
    validate_zarr_output(out)
    return out


def parse_product_spec(raw: Mapping[str, object]) -> ProductSpec:
    _check_keys("ProductSpec", frozenset(ProductSpec.__dataclass_fields__), raw)
    prod = ProductSpec(
        product_id=_req_str(raw, "product_id"),
        model_id=_req_str(raw, "model_id"),
        label=_req_str(raw, "label"),
        region_tz=_req_str(raw, "region_tz"),
        domain_profile=_req_str(raw, "domain_profile"),
        science_profile=_req_str(raw, "science_profile"),
        primary_source_profile=_req_str(raw, "primary_source_profile"),
        candidate_source_profile=_req_str(raw, "candidate_source_profile"),
        output_profile=_req_str(raw, "output_profile"),
        spinup_hours=_req_int(raw, "spinup_hours"),
        forcing_cycle_hour_utc=_req_int(raw, "forcing_cycle_hour_utc"),
        publish_origin_hour_utc=_req_int(raw, "publish_origin_hour_utc"),
        local_deadline_hhmm=_req_str(raw, "local_deadline_hhmm"),
        min_horizon_hours=_req_int(raw, "min_horizon_hours"),
        max_cycle_age_hours=_req_int(raw, "max_cycle_age_hours"),
        schedule_cron_utc=_req_str(raw, "schedule_cron_utc"),
    )
    if not (0 <= prod.forcing_cycle_hour_utc < 24 and 0 <= prod.publish_origin_hour_utc < 24):
        raise ValueError("UTC cycle hours must lie in 0..23")
    return prod


__all__ = [
    "parse_domain_profile",
    "parse_forcing_profile",
    "parse_output_profile",
    "parse_product_spec",
    "parse_science_profile",
]
