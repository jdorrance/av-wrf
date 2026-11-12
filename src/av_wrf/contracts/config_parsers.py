"""Strict declarative dictionary-to-dataclass parsers with key, type, and range checks."""

from __future__ import annotations

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


def _check_keys(name: str, expected: frozenset[str], raw: Mapping[str, object]) -> None:
    unknown = sorted(set(raw.keys()) - expected)
    missing = sorted(expected - set(raw.keys()))
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


def parse_domain_profile(raw: Mapping[str, object]) -> DomainProfile:
    _check_keys("DomainProfile", frozenset(DomainProfile.__dataclass_fields__), raw)
    dom = DomainProfile(
        _req_str(raw, "profile_id"),
        _req_float(raw, "earth_radius_m"),
        _req_float(raw, "center_lat_deg"),
        _req_float(raw, "center_lon_deg"),
        _req_float(raw, "std_parallel_1_deg"),
        _req_float(raw, "std_parallel_2_deg"),
        _req_float(raw, "central_meridian_deg"),
        _req_float(raw, "dx_m"),
        _req_float(raw, "dy_m"),
        _req_int(raw, "e_we"),
        _req_int(raw, "e_sn"),
        _req_int(raw, "bdy_trim_cells"),
        _req_int(raw, "e_vert"),
        _req_int(raw, "exported_levels_k"),
        _req_float(raw, "p_top_pa"),
        _req_int(raw, "auto_levels_opt"),
        _req_float(raw, "dzbot_m"),
        _req_float(raw, "dzstretch_s"),
        _req_float(raw, "dzstretch_u"),
        _req_float(raw, "max_dz_m"),
        _req_int(raw, "min_bl_levels_2km_agl"),
        _req_float_tuple(raw, "terrain_elevations_m"),
        _req_str(raw, "dem_source"),
        _req_str(raw, "landuse_source"),
        _req_str(raw, "geog_data_path"),
    )
    if dom.earth_radius_m <= 0.0 or dom.dx_m <= 0.0 or dom.dy_m <= 0.0 or dom.p_top_pa <= 0.0:
        raise ValueError("Domain radius, grid spacing, and p_top_pa must be positive")
    if any(elev < 0.0 for elev in dom.terrain_elevations_m):
        raise ValueError("terrain_elevations_m entries must be >= 0.0 m")
    return dom


def parse_science_profile(raw: Mapping[str, object]) -> ScienceProfile:
    _check_keys("ScienceProfile", frozenset(ScienceProfile.__dataclass_fields__), raw)
    return ScienceProfile(
        _req_str(raw, "profile_id"),
        _req_str(raw, "wrf_version"),
        _req_str(raw, "wps_version"),
        _req_int(raw, "mp_physics"),
        _req_int(raw, "ra_lw_physics"),
        _req_int(raw, "ra_sw_physics"),
        _req_int(raw, "radt_min"),
        _req_int(raw, "slope_rad"),
        _req_int(raw, "topo_shading"),
        _req_float(raw, "shadlen_m"),
        _req_int(raw, "sf_surface_physics"),
        _req_int(raw, "opt_runsrf"),
        _req_int(raw, "opt_runsub"),
        _req_int(raw, "bl_pbl_physics"),
        _req_int(raw, "sf_sfclay_physics"),
        _req_int(raw, "topo_wind"),
        _req_int(raw, "ysu_topdown_pblmix"),
        _req_bool(raw, "shinhong_scu_mixing"),
        _req_bool(raw, "shinhong_nonlocal_flux"),
        _req_int(raw, "cu_physics"),
        _req_int(raw, "gwd_opt"),
        _req_bool(raw, "gwd_if_nonhyd"),
        _req_int(raw, "use_theta_m"),
        _req_float(raw, "base_temp_k"),
        _req_int(raw, "hybrid_opt"),
        _req_float(raw, "etac"),
        _req_int(raw, "diff_opt"),
        _req_int(raw, "km_opt"),
        _req_int(raw, "diff_6th_opt"),
        _req_float(raw, "diff_6th_factor"),
        _req_float(raw, "epssm"),
        _req_int(raw, "w_damping"),
        _req_int(raw, "damp_opt"),
        _req_float(raw, "zdamp_m"),
        _req_float(raw, "dampcoef"),
        _req_int(raw, "time_step_s"),
        _req_int(raw, "spec_bdy_width"),
        _req_int(raw, "spec_zone"),
        _req_int(raw, "relax_zone"),
        _req_int(raw, "afwa_diag_opt"),
        _req_int(raw, "afwa_buoy_opt"),
        _req_int(raw, "history_interval_min"),
        _req_float(raw, "bucket_mm"),
    )


def parse_forcing_profile(raw: Mapping[str, object]) -> ForcingProfile:
    levels_key = "metgrid_levels" if "metgrid_levels" in raw else "pressure_level_count"
    soil_key = "metgrid_soil_levels" if "metgrid_soil_levels" in raw else "soil_layer_centers_cm"
    keys = frozenset(ForcingProfile.__dataclass_fields__) - {
        "metgrid_levels",
        "metgrid_soil_levels",
        "use_sh_qv",
    }
    if "use_sh_qv" in raw:
        keys |= {"use_sh_qv"}
    _check_keys("ForcingProfile", keys | {levels_key, soil_key}, raw)
    soil_levels = (
        _req_int(raw, soil_key)
        if soil_key == "metgrid_soil_levels"
        else len(_req_float_tuple(raw, soil_key))
    )
    prof = ForcingProfile(
        _req_str(raw, "profile_id"),
        _req_str(raw, "provider"),
        _req_str(raw, "vtable_name"),
        _req_int(raw, "interval_seconds"),
        _req_float(raw, "grid_spacing_deg"),
        _req_int(raw, levels_key),
        soil_levels,
        _req_float(raw, "source_ready_lag_p95_hours"),
        _req_str_tuple(raw, "mirrors"),
        _req_bool(raw, "fail_closed"),
        _req_bool(raw, "use_sh_qv") if "use_sh_qv" in raw else False,
    )
    if not re.fullmatch(r"Vtable\.[A-Za-z0-9][A-Za-z0-9_.-]*", prof.vtable_name):
        raise ValueError("ForcingProfile.vtable_name must be a single WPS Vtable filename")
    if prof.interval_seconds < 3600 or prof.interval_seconds % 3600 != 0:
        raise ValueError("ForcingProfile.interval_seconds must be a positive multiple of 3600")
    if prof.metgrid_levels < 10 or prof.metgrid_soil_levels < 1:
        raise ValueError("ForcingProfile requires at least 10 metgrid levels and one soil layer")
    if prof.source_ready_lag_p95_hours <= 0.0:
        raise ValueError("ForcingProfile requires positive source ready lag")
    return prof


def parse_output_profile(raw: Mapping[str, object]) -> OutputProfile:
    _check_keys("OutputProfile", frozenset(OutputProfile.__dataclass_fields__), raw)
    out = OutputProfile(
        _req_str(raw, "profile_id"),
        _req_str(raw, "schema_id"),
        _req_int(raw, "map_end_hour"),
        _req_int(raw, "hourly_through_hour"),
        _req_int(raw, "tail_step_hours"),
        _req_int(raw, "shard_time_steps"),
        _req_int_pair(raw, "inner_chunk_yx"),
        _req_int(raw, "zstd_level"),
    )
    if out.shard_time_steps < 1 or not (1 <= out.zstd_level <= 22):
        raise ValueError("OutputProfile requires shard_time_steps >= 1 and zstd_level in 1..22")
    return out


def parse_product_spec(raw: Mapping[str, object]) -> ProductSpec:
    _check_keys("ProductSpec", frozenset(ProductSpec.__dataclass_fields__), raw)
    prod = ProductSpec(
        _req_str(raw, "product_id"),
        _req_str(raw, "model_id"),
        _req_str(raw, "label"),
        _req_str(raw, "region_tz"),
        _req_str(raw, "domain_profile"),
        _req_str(raw, "science_profile"),
        _req_str(raw, "primary_source_profile"),
        _req_str(raw, "candidate_source_profile"),
        _req_str(raw, "output_profile"),
        _req_int(raw, "spinup_hours"),
        _req_int(raw, "forcing_cycle_hour_utc"),
        _req_int(raw, "publish_origin_hour_utc"),
        _req_str(raw, "local_deadline_hhmm"),
        _req_int(raw, "min_horizon_hours"),
        _req_int(raw, "max_cycle_age_hours"),
        _req_str(raw, "schedule_cron_utc"),
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
