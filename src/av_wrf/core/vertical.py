"""Vertical auto_levels_opt=2 + hybrid_opt=2 column estimation, solver ZNW check, and gates.

Assumptions & High-Mountain Coordinate Math (Skamarock et al. 2019 NCAR/TN-556+STR):
1. `real.exe` (`auto_levels_opt=2`, `dyn_em/module_initialize_real.F`) stretches `eta` levels
   in dry log-pressure space from `dzbot` up to `max_dz` (`(max_dz/2 - dz) / (max_dz/2)` blend
   from `dzstretch_s` to `dzstretch_u`) with `T0 = 300 K` (`WRF_T0_K`) and isothermal stratosphere
   floor (`215 K`), failing with `not enough eta levels to reach p_top` if `e_vert` is too small
   (e.g. `51` faces fails at `50 hPa`, while `55` faces passes). `validate_solver_znw` checks
   actual solver-produced `ZNW` arrays directly when `wrfinput` / `wrfout` are available.
2. Over elevated terrain (`terrain_m > 0`), `hybrid_opt = 2` (`etac`) maps `eta` via cubic `B(eta)`
   and checks `dp_d > 0` across every configured `terrain_elevations_m`.
3. `validate_frame_columns` verifies finite values, positive strictly-ordered thicknesses within
   `CONSUMER_MAX_PROFILE_GAP_M = 1,000 m`, `0-2 km` BL level counts, and output-frame Courant
   proxy (`max_cfl <= MAX_ADMITTED_CFL`; actual RK3 substep stability is gated on solver exit).

References:
- WRF level construction:
  https://github.com/wrf-model/WRF/blob/v4.8.0/dyn_em/module_initialize_real.F
- WRF auto_levels_opt = 2:
  https://www2.mmm.ucar.edu/wrf/site/users_guide/real_initialization.html
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from av_wrf.contracts import (
    BL_CEILING_AGL_M,
    CONSUMER_MAX_PROFILE_GAP_M,
    MAX_ADMITTED_CFL,
    MAX_EXPORTED_LEVELS,
    DomainProfile,
    NormalizedWrfFrame,
)
from av_wrf.core.thermo import G0_MPS2, P0_PA, R_DRY_J_KG_K

MIN_STABLE_DZBOT_M = 25.0
MAX_SAFE_REFERENCE_DZ_M = 900.0
REF_T0_K = 290.0
REF_LAPSE_A_K = 50.0
REF_TISO_K = 215.0


def _ref_temp_k(p_pa: float) -> float:
    return max(REF_TISO_K, REF_T0_K + REF_LAPSE_A_K * math.log(max(1.0, p_pa) / P0_PA))


def _hybrid_b(eta: float, etac: float) -> float:
    if eta <= etac:
        return 0.0
    denom = (1.0 - etac) ** 3
    c1 = 2.0 * (etac**2) / denom
    c2 = -etac * (4.0 + etac + etac**2) / denom
    c3 = 2.0 * (1.0 + etac + etac**2) / denom
    c4 = -(1.0 + etac) / denom
    return c1 + c2 * eta + c3 * (eta**2) + c4 * (eta**3)


def _reference_eta_faces(domain: DomainProfile) -> list[float]:
    """Mirror `real.exe` `auto_levels_opt=2` log-P stretching from `dzbot` up to `max_dz`."""
    n_mass = domain.e_vert - 1
    half_max = 0.5 * domain.max_dz_m
    dlnp = (G0_MPS2 * domain.dzbot_m) / (R_DRY_J_KG_K * REF_T0_K)
    p_cur = P0_PA
    dlnp_steps: list[float] = []
    capped_indices: list[int] = []
    for idx in range(n_mass):
        t_k = _ref_temp_k(p_cur)
        max_step = (G0_MPS2 * domain.max_dz_m) / (R_DRY_J_KG_K * t_k)
        if dlnp >= max_step - 1e-9:
            dlnp = max_step
            capped_indices.append(idx)
        dz_k = (R_DRY_J_KG_K * t_k / G0_MPS2) * dlnp
        dlnp_steps.append(dlnp)
        p_cur *= math.exp(-dlnp)
        weight_s = max(0.0, (half_max - dz_k) / max(1.0, half_max))
        stretch = domain.dzstretch_u + weight_s * (domain.dzstretch_s - domain.dzstretch_u)
        dlnp *= stretch

    target_lnp = math.log(P0_PA / domain.p_top_pa)
    total_lnp = sum(dlnp_steps)
    if total_lnp < target_lnp:
        raise ValueError(
            f"not enough eta levels (e_vert={domain.e_vert}) to reach p_top_pa={domain.p_top_pa}"
        )
    excess = total_lnp - target_lnp
    capped_sum = sum(dlnp_steps[i] for i in capped_indices)
    if capped_indices and capped_sum > excess:
        scale = (capped_sum - excess) / capped_sum
        for idx in capped_indices:
            dlnp_steps[idx] *= scale
    else:
        scale = target_lnp / total_lnp
        dlnp_steps = [s * scale for s in dlnp_steps]

    etas = [1.0]
    p_walk = P0_PA
    for step in dlnp_steps[:-1]:
        p_walk *= math.exp(-step)
        etas.append((p_walk - domain.p_top_pa) / (P0_PA - domain.p_top_pa))
    etas.append(0.0)
    return etas


def _midpoints_from_eta_faces(
    etas: Sequence[float], domain: DomainProfile, terrain_m: float, etac: float
) -> tuple[float, ...]:
    phi_sfc = G0_MPS2 * max(0.0, terrain_m)
    radicand = ((REF_T0_K / REF_LAPSE_A_K) ** 2) - (2.0 * phi_sfc / (REF_LAPSE_A_K * R_DRY_J_KG_K))
    if radicand <= 0.0:
        raise ValueError("terrain_m exceeds dry reference atmosphere ceiling")
    ps_ref = P0_PA * math.exp(-(REF_T0_K / REF_LAPSE_A_K) + math.sqrt(radicand))
    if ps_ref <= domain.p_top_pa * 1.2:
        raise ValueError("Reference surface pressure at terrain_m is too close to p_top_pa")
    pt = domain.p_top_pa
    z_face = 0.0
    midpoints: list[float] = []
    b0 = _hybrid_b(etas[0], etac)
    p_bot = b0 * (ps_ref - pt) + (etas[0] - b0) * (P0_PA - pt) + pt
    for k in range(len(etas) - 1):
        b_top = _hybrid_b(etas[k + 1], etac)
        p_top_k = b_top * (ps_ref - pt) + (etas[k + 1] - b_top) * (P0_PA - pt) + pt
        if p_top_k >= p_bot:
            raise ValueError(
                f"Hybrid vertical coordinate folded at level {k} over {terrain_m:.0f} m; "
                f"lower etac ({etac})"
            )
        p_mid = 0.5 * (p_bot + p_top_k)
        dz_k = (R_DRY_J_KG_K * _ref_temp_k(p_mid) / G0_MPS2) * math.log(p_bot / p_top_k)
        midpoints.append(z_face + 0.5 * dz_k)
        z_face += dz_k
        p_bot = p_top_k
    return tuple(midpoints)


def estimate_auto_levels_agl_m(
    domain: DomainProfile,
    terrain_m: float = 0.0,
    *,
    etac: float = 0.2,
) -> tuple[float, ...]:
    """Estimate WRF auto_levels_opt=2 + hybrid_opt=2 mass-midpoint AGL heights over terrain_m."""
    if domain.auto_levels_opt != 2:
        raise ValueError("Only auto_levels_opt=2 is supported")
    if domain.dzbot_m < MIN_STABLE_DZBOT_M:
        raise ValueError(f"dzbot_m ({domain.dzbot_m}) must be >= {MIN_STABLE_DZBOT_M} m")
    if not (0.0 <= etac < 0.7):
        raise ValueError(f"etac ({etac}) must be in [0.0, 0.7)")
    if domain.e_vert - 1 < domain.exported_levels_k:
        raise ValueError("e_vert - 1 must be >= exported_levels_k")
    etas = _reference_eta_faces(domain)
    return _midpoints_from_eta_faces(etas, domain, terrain_m, etac)


def validate_solver_znw(
    znw: Sequence[float],
    domain: DomainProfile,
    *,
    etac: float = 0.2,
) -> int:
    """Validate solver-emitted `ZNW` full eta faces across configured domain elevations."""
    if len(znw) != domain.e_vert:
        raise ValueError(f"Solver ZNW length {len(znw)} != configured e_vert {domain.e_vert}")
    if abs(znw[0] - 1.0) > 1e-5 or abs(znw[-1]) > 1e-5:
        raise ValueError("Solver ZNW must span [1.0, 0.0]")
    for idx in range(1, len(znw)):
        if not math.isfinite(znw[idx]) or znw[idx] >= znw[idx - 1]:
            raise ValueError(f"Non-monotonic solver ZNW at index {idx}")
    counts = [
        validate_vertical_column(_midpoints_from_eta_faces(znw, domain, elev, etac), domain)
        for elev in domain.terrain_elevations_m
    ]
    return min(counts)


def _check_column_bounds(midpoints_agl_m: Sequence[float], domain: DomainProfile) -> None:
    if domain.dzbot_m < MIN_STABLE_DZBOT_M:
        raise ValueError(f"dzbot_m ({domain.dzbot_m}) must be >= {MIN_STABLE_DZBOT_M} m")
    if domain.max_dz_m > MAX_SAFE_REFERENCE_DZ_M:
        raise ValueError(
            f"max_dz_m ({domain.max_dz_m}) must be <= {MAX_SAFE_REFERENCE_DZ_M} m so summer "
            "thermal expansion never trips the consumer 1000 m profile-gap rejection"
        )
    if domain.exported_levels_k > MAX_EXPORTED_LEVELS or domain.exported_levels_k >= domain.e_vert:
        raise ValueError("exported_levels_k exceeds contract or computational level count")
    if len(midpoints_agl_m) < domain.exported_levels_k:
        raise ValueError("Column has fewer levels than exported_levels_k")
    if not math.isfinite(midpoints_agl_m[0]) or not (0.0 < midpoints_agl_m[0] <= domain.max_dz_m):
        raise ValueError("Lowest mass level midpoint must lie in (0, max_dz_m] AGL")


def validate_vertical_column(
    midpoints_agl_m: Sequence[float],
    domain: DomainProfile,
) -> int:
    """Verify monotonic levels, dzbot, thermal-headroom max_dz, and BL count; return BL count."""
    _check_column_bounds(midpoints_agl_m, domain)
    bl_count = 1 if midpoints_agl_m[0] <= BL_CEILING_AGL_M else 0
    for idx in range(1, domain.exported_levels_k):
        if not math.isfinite(midpoints_agl_m[idx]):
            raise ValueError(f"Non-finite vertical level at index {idx}")
        dz = midpoints_agl_m[idx] - midpoints_agl_m[idx - 1]
        if dz <= 0.0:
            raise ValueError(f"Non-monotonic vertical levels at index {idx}")
        if dz > domain.max_dz_m + 1e-6:
            raise ValueError(f"Vertical gap {dz:.1f} m at level {idx} exceeds {domain.max_dz_m} m")
        if midpoints_agl_m[idx] <= BL_CEILING_AGL_M:
            bl_count += 1
    if bl_count < domain.min_bl_levels_2km_agl:
        raise ValueError(
            f"Only {bl_count} mass levels in 0-2000 m AGL; "
            f"requires >= {domain.min_bl_levels_2km_agl}"
        )
    return bl_count


def local_cfl_3d(
    u_mps: float,
    v_mps: float,
    w_mps: float,
    dz_m: float,
    dx_m: float,
    dy_m: float,
    dt_s: float,
) -> float:
    """Compute output-frame 3D Courant proxy for one exported mass cell."""
    if not all(math.isfinite(x) for x in (u_mps, v_mps, w_mps, dz_m, dx_m, dy_m, dt_s)):
        raise ValueError("Non-finite input to local_cfl_3d")
    return (abs(u_mps) / dx_m + abs(v_mps) / dy_m + abs(w_mps) / max(10.0, dz_m)) * dt_s


def validate_frame_columns(frame: NormalizedWrfFrame, domain: DomainProfile) -> float:
    """Verify finite full-domain exported AGL gaps, 0-2 km BL counts, and Courant proxy ceiling."""
    if not (
        math.isfinite(frame.min_dz_m)
        and math.isfinite(frame.max_dz_m)
        and math.isfinite(frame.max_cfl)
    ):
        raise ValueError("Non-finite column gap or Courant proxy in NormalizedWrfFrame")
    if frame.min_dz_m <= 0.0 or frame.max_dz_m > CONSUMER_MAX_PROFILE_GAP_M:
        raise ValueError(
            f"Exported column gap [{frame.min_dz_m:.1f}, {frame.max_dz_m:.1f}] m "
            f"violates (0, {CONSUMER_MAX_PROFILE_GAP_M}] m"
        )
    if frame.min_bl_levels_2km < domain.min_bl_levels_2km_agl:
        raise ValueError(
            f"Exported BL count {frame.min_bl_levels_2km} < {domain.min_bl_levels_2km_agl}"
        )
    if frame.max_cfl < 0.0 or frame.max_cfl > MAX_ADMITTED_CFL:
        raise ValueError(f"Exported frame max_cfl {frame.max_cfl:.4f} > {MAX_ADMITTED_CFL}")
    return round(frame.max_cfl, 5)


__all__ = [
    "BL_CEILING_AGL_M",
    "CONSUMER_MAX_PROFILE_GAP_M",
    "MAX_ADMITTED_CFL",
    "MAX_SAFE_REFERENCE_DZ_M",
    "MIN_STABLE_DZBOT_M",
    "estimate_auto_levels_agl_m",
    "local_cfl_3d",
    "validate_frame_columns",
    "validate_solver_znw",
    "validate_vertical_column",
]
