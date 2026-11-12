"""WRF v4.8 physics/dynamics namelist sections and preflight validation."""

from __future__ import annotations

from av_wrf.contracts import DomainProfile, ScienceProfile
from av_wrf.core.grid import (
    computational_latlon_bounds,
    lambert_map_scale,
    validate_domain_profile,
)

NamelistScalar = int | float | str | bool
NamelistValue = NamelistScalar | tuple[NamelistScalar, ...]
NamelistSections = dict[str, dict[str, NamelistValue]]


def per_domain(value: NamelistScalar, max_dom: int) -> NamelistValue:
    """Repeat a `max_domains` Registry entry; WRF rejects a list on a scalar entry."""
    return (value, value) if max_dom > 1 else value


def _validate_dynamics(science: ScienceProfile, domain: DomainProfile) -> None:
    if science.spec_bdy_width != science.spec_zone + science.relax_zone:
        raise ValueError("spec_bdy_width must equal spec_zone + relax_zone")
    if domain.bdy_trim_cells <= science.spec_bdy_width:
        raise ValueError("bdy_trim_cells must exceed spec_bdy_width")
    # https://www2.mmm.ucar.edu/wrf/users/docs/technote/v4_technote.pdf
    # Lambert scale has a single latitude minimum; its maximum is at a footprint extreme.
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
    max_dt_s = 6.0 * ((domain.dx_m / m_max) / 1000.0)
    if not 0 < science.time_step_s <= max_dt_s:
        raise ValueError(
            f"time_step_s ({science.time_step_s}) exceeds 6*dx_phys ({max_dt_s:.2f}s, "
            f"m_max={m_max:.4f})"
        )
    if science.use_adaptive_time_step and not (
        0 < science.min_time_step_s <= science.time_step_s <= science.max_time_step_s <= max_dt_s
    ):
        raise ValueError(f"Adaptive steps require 0 < min <= start <= max <= {max_dt_s:.2f}s")


def _validate_physics_compatibility(science: ScienceProfile) -> None:  # noqa: C901
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
        or science.cu_physics == 11
    ):
        raise ValueError(
            "km_opt=5 requires diff_opt=2, bl_pbl_physics=0, "
            "sf_sfclay_physics in (0, 1, 5, 91), and cu_physics other than 11"
        )
    # https://github.com/wrf-model/WRF/blob/v4.8.0/share/module_check_a_mundo.F#L2114-L2128
    if (science.bl_pbl_physics == 10) != (science.sf_sfclay_physics == 10):
        raise ValueError("TEMF requires both bl_pbl_physics=10 and sf_sfclay_physics=10")
    # MYNN EDMF and YSU top-down mixing are scheme-exclusive (module_check_a_mundo.F).
    if science.bl_pbl_physics == 5:
        if science.sf_sfclay_physics != 5:
            raise ValueError("MYNN2.5-EDMF requires sf_sfclay_physics=5")
        if science.topo_wind or science.ysu_topdown_pblmix:
            raise ValueError("topo_wind/ysu_topdown_pblmix belong to YSU (bl_pbl_physics=1)")
        # WRF 4.8 couples MYNN clouds only inside its icloud=1 radiation branch.
        # https://github.com/wrf-model/WRF/blob/v4.8.0/phys/module_radiation_driver.F#L1320-L1428
        if science.icloud_bl and science.icloud != 1:
            raise ValueError("MYNN cloud radiation coupling requires icloud=1")
    elif science.bl_pbl_physics == 1:
        if science.sf_sfclay_physics != 1:
            raise ValueError("YSU requires sf_sfclay_physics=1")
        if science.bl_mynn_edmf or science.icloud_bl:
            raise ValueError("bl_mynn_edmf and icloud_bl require bl_pbl_physics=5")
    elif science.bl_mynn_edmf or science.icloud_bl:
        raise ValueError("MYNN EDMF options require bl_pbl_physics=5")
    if science.diff_6th_opt == 2 and not science.diff_6th_slopeopt:
        raise ValueError("diff_6th_opt=2 over steep terrain requires diff_6th_slopeopt=1")


def validate_science_profile(science: ScienceProfile, domain: DomainProfile) -> None:
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
    if science.icloud not in (1, 3):
        raise ValueError("icloud must be 1 (Xu-Randall) or 3 (Thompson scale-aware)")
    if science.sf_lake_physics == 1 and not (
        science.use_lakedepth in (0, 1) and science.lakedepth_default_m > 0.0
    ):
        raise ValueError("sf_lake_physics=1 requires use_lakedepth in (0, 1) and positive depth")
    _validate_dynamics(science, domain)


def _runtime_physics(science: ScienceProfile, max_dom: int) -> dict[str, NamelistValue]:
    options: dict[str, NamelistValue] = {}
    if science.mp_physics == 8:
        # Package-qualified tables must be read; missing caches fail instead of rebuilding.
        options.update(force_read_thompson=True, write_thompson_tables=False)
    if science.sf_lake_physics == 1:
        # Registry/registry.lake is per domain; a scalar leaves the nest at defaults.
        options.update(
            use_lakedepth=per_domain(science.use_lakedepth, max_dom),
            lakedepth_default=per_domain(science.lakedepth_default_m, max_dom),
            lake_min_elev=per_domain(science.lake_min_elev_m, max_dom),
        )
    return options


def physics_and_dynamics_sections(science: ScienceProfile, max_dom: int) -> NamelistSections:
    physics: dict[str, NamelistValue] = {
        # `max_domains` Registry entries take one value per domain; scalars take one total.
        "mp_physics": per_domain(science.mp_physics, max_dom),
        "cu_physics": per_domain(science.cu_physics, max_dom),
        "ra_lw_physics": per_domain(science.ra_lw_physics, max_dom),
        "ra_sw_physics": per_domain(science.ra_sw_physics, max_dom),
        "radt": per_domain(science.radt_min, max_dom),
        "slope_rad": per_domain(science.slope_rad, max_dom),
        "topo_shading": per_domain(science.topo_shading, max_dom),
        "shadlen": science.shadlen_m,
        "sf_sfclay_physics": per_domain(science.sf_sfclay_physics, max_dom),
        "sf_surface_physics": per_domain(science.sf_surface_physics, max_dom),
        "bl_pbl_physics": per_domain(science.bl_pbl_physics, max_dom),
        "bl_mynn_edmf": per_domain(science.bl_mynn_edmf, max_dom),
        "topo_wind": per_domain(science.topo_wind, max_dom),
        "ysu_topdown_pblmix": science.ysu_topdown_pblmix,
        "icloud": science.icloud,
        "icloud_bl": science.icloud_bl,
        "sf_lake_physics": per_domain(science.sf_lake_physics, max_dom),
        "shinhong_scu_mixing": per_domain(science.shinhong_scu_mixing, max_dom),
        "shinhong_nonlocal_flux": per_domain(science.shinhong_nonlocal_flux, max_dom),
        "bucket_mm": science.bucket_mm,
    }
    physics.update(_runtime_physics(science, max_dom))
    dynamics: dict[str, NamelistValue] = {
        "use_theta_m": science.use_theta_m,
        "base_temp": science.base_temp_k,
        "hybrid_opt": science.hybrid_opt,
        "etac": science.etac,
        "diff_opt": per_domain(science.diff_opt, max_dom),
        "km_opt": per_domain(science.km_opt, max_dom),
        "diff_6th_opt": per_domain(science.diff_6th_opt, max_dom),
        "diff_6th_factor": per_domain(science.diff_6th_factor, max_dom),
        "diff_6th_slopeopt": per_domain(science.diff_6th_slopeopt, max_dom),
        "diff_6th_thresh": per_domain(science.diff_6th_thresh, max_dom),
        "epssm": per_domain(science.epssm, max_dom),
        "w_damping": science.w_damping,
        "damp_opt": science.damp_opt,
        "zdamp": per_domain(science.zdamp_m, max_dom),
        "dampcoef": per_domain(science.dampcoef, max_dom),
        "gwd_opt": per_domain(science.gwd_opt, max_dom),
        "gwd_if_nonhyd": per_domain(science.gwd_if_nonhyd, max_dom),
    }
    return {
        "physics": physics,
        "noah_mp": {"opt_runsrf": science.opt_runsrf, "opt_runsub": science.opt_runsub},
        "dynamics": dynamics,
        "afwa": {
            "afwa_diag_opt": science.afwa_diag_opt,
            "afwa_buoy_opt": science.afwa_buoy_opt,
        },
    }


def adaptive_domains(science: ScienceProfile, ratio: int) -> dict[str, NamelistValue]:
    """Build WRF adaptive-RK3 keys with Registry-correct scalar/max_domains shapes."""
    # https://github.com/wrf-model/WRF/blob/v4.8.0/run/README.namelist
    nested = ratio > 1
    max_dom = 2 if nested else 1

    def step(value: int) -> NamelistValue:
        return (ratio * value, value) if nested else value

    return {
        "use_adaptive_time_step": science.use_adaptive_time_step,
        "step_to_output_time": True,
        "adaptation_domain": max_dom,
        "target_cfl": per_domain(science.target_cfl, max_dom),
        "target_hcfl": per_domain(science.target_hcfl, max_dom),
        "max_step_increase_pct": (5, 51) if nested else 51,
        "starting_time_step": step(science.time_step_s),
        "min_time_step": step(science.min_time_step_s),
        "max_time_step": step(science.max_time_step_s),
    }


__all__ = [
    "NamelistScalar",
    "NamelistSections",
    "NamelistValue",
    "adaptive_domains",
    "per_domain",
    "physics_and_dynamics_sections",
    "validate_science_profile",
]
