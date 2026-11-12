"""Vertical auto_levels_opt=2 + hybrid_opt=2 column estimation, solver ZNW check, and gates.

Assumptions & High-Mountain Coordinate Math (Skamarock et al. 2019 NCAR/TN-556+STR):
1. `real.exe` (`auto_levels_opt=2`, `dyn_em/module_initialize_real.F`, `SUBROUTINE levels`)
   stretches `eta` faces using a constant isothermal scale-height temperature `tt = 290.0 K`,
   clamped `[0, 1]` `(dzmax/2 - dz) / (dzmax/2)` blend from `dzstretch_s` to `dzstretch_u`,
   and constant-`dz` upper fill once remaining mean layer thickness `< dz`, pinning
   `eta[e_vert - 1] = 0.0`. WRF limits the upper fill to `1.5 * max_dz`.
2. Reference column heights over elevated `terrain_m` (`init_domain_rk`) use `base_temp_k`,
   the log-pressure lapse, and cubic hybrid `B(eta)` with WRF's default isothermal upper profile.
3. Consumer `windy-av-layer` (`NATIVE_DIAGNOSTIC_PARAMETERS.minimumLevelAglM = 20 m`) discards
   any model level at or below `20.0 m` AGL. Both `validate_vertical_column` and
   `validate_frame_columns` require `level[0] > 20.0 m` AGL and count only usable levels in
   `(20.0, 2000.0]` m AGL toward `min_bl_levels_2km_agl`.
4. Solver integration stability is gated on `time_step <= 6 * dx_phys`, `rsl.error.*` CFL checks,
   and `wrf.exe` completion; output-frame `max_cfl` is recorded as a finite non-negative diagnostic.

References:
- WRF level construction:
  https://github.com/wrf-model/WRF/blob/v4.8.0/dyn_em/module_initialize_real.F
- WRF auto_levels_opt = 2:
  https://www2.mmm.ucar.edu/wrf/site/users_guide/real_initialization.html
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np
import numpy.typing as npt

from av_wrf.contracts import (
    BL_CEILING_AGL_M,
    CONSUMER_MAX_PROFILE_GAP_M,
    MAX_EXPORTED_LEVELS,
    DomainProfile,
    NormalizedWrfFrame,
)
from av_wrf.core.thermo import G0_MPS2, P0_PA, R_DRY_J_KG_K

MIN_STABLE_DZBOT_M = 25.0
CONSUMER_MIN_LEVEL_AGL_M = 20.0
MAX_SAFE_REFERENCE_DZ_M = 900.0
LEVELS_SCALE_TEMP_K = 290.0
# WRF 4.8 uses a 200 K floor and disables the separate lapse branch with base_pres_strat=0.
# https://github.com/wrf-model/WRF/blob/v4.8.0/Registry/Registry.EM_COMMON
REF_LAPSE_A_K = 50.0
REF_TISO_K = 200.0


def _ref_temp_k(p_pa: float, base_temp_k: float) -> float:
    return max(REF_TISO_K, base_temp_k + REF_LAPSE_A_K * math.log(max(1.0, p_pa) / P0_PA))


def _hybrid_b(eta: float, etac: float) -> float:
    if eta <= etac:
        return 0.0
    denom = (1.0 - etac) ** 3
    c1 = 2.0 * (etac**2) / denom
    c2 = -etac * (4.0 + etac + etac**2) / denom
    c3 = 2.0 * (1.0 + etac + etac**2) / denom
    c4 = -(1.0 + etac) / denom
    return c1 + c2 * eta + c3 * (eta**2) + c4 * (eta**3)


def auto_eta_faces(domain: DomainProfile) -> list[float]:
    """Calculate full eta faces with WRF v4.8 `levels`, using 290 K isothermal scaling."""
    n_mass = domain.e_vert - 1
    scale_h_m = (R_DRY_J_KG_K * LEVELS_SCALE_TEMP_K) / G0_MPS2
    z_top_m = scale_h_m * math.log(P0_PA / domain.p_top_pa)
    half_max = 0.5 * domain.max_dz_m
    dz = domain.dzbot_m
    if n_mass < 2 or not 0.0 < dz < z_top_m or half_max <= 0.0:
        raise ValueError("Invalid WRF automatic level geometry")
    faces_m = [0.0, dz]
    for remaining in range(n_mass - 1, 0, -1):
        # `levels` applies max_dz through the blend and the upper fill check.
        weight_s = max((half_max - dz) / half_max, 0.0)
        dz *= domain.dzstretch_u + weight_s * (domain.dzstretch_s - domain.dzstretch_u)
        upper_dz = (z_top_m - faces_m[-1]) / remaining
        if upper_dz < dz:
            if upper_dz > 1.5 * domain.max_dz_m:
                raise ValueError(f"WRF upper eta levels too thick ({upper_dz:.1f} m)")
            start_m = faces_m[-1]
            faces_m.extend(start_m + upper_dz * i for i in range(1, remaining))
            break
        faces_m.append(faces_m[-1] + dz)
    else:
        raise ValueError(
            f"not enough eta levels (e_vert={domain.e_vert}) to reach p_top_pa={domain.p_top_pa}"
        )
    pt = domain.p_top_pa
    return [(P0_PA * math.exp(-z_m / scale_h_m) - pt) / (P0_PA - pt) for z_m in faces_m] + [0.0]


def _midpoints_from_eta_faces(
    etas: Sequence[float],
    domain: DomainProfile,
    terrain_m: float,
    etac: float,
    base_temp_k: float,
) -> tuple[float, ...]:
    phi_sfc = G0_MPS2 * terrain_m
    radicand = ((base_temp_k / REF_LAPSE_A_K) ** 2) - (
        2.0 * phi_sfc / (REF_LAPSE_A_K * R_DRY_J_KG_K)
    )
    if radicand <= 0.0:
        raise ValueError("terrain_m exceeds dry reference atmosphere ceiling")
    ps_ref = P0_PA * math.exp(-(base_temp_k / REF_LAPSE_A_K) + math.sqrt(radicand))
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
        dz_k = (R_DRY_J_KG_K * _ref_temp_k(p_mid, base_temp_k) / G0_MPS2) * math.log(
            p_bot / p_top_k
        )
        midpoints.append(z_face + 0.5 * dz_k)
        z_face += dz_k
        p_bot = p_top_k
    return tuple(midpoints)


def estimate_auto_levels_agl_m(
    domain: DomainProfile,
    terrain_m: float = 0.0,
    *,
    etac: float = 0.2,
    base_temp_k: float = 300.0,
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
    etas = auto_eta_faces(domain)
    return _midpoints_from_eta_faces(etas, domain, terrain_m, etac, base_temp_k)


def validate_solver_znw(znw: Sequence[float], domain: DomainProfile) -> None:
    """Check that solver-emitted full eta faces match the configured WRF grid."""
    if len(znw) != domain.e_vert:
        raise ValueError(f"Solver ZNW length {len(znw)} != configured e_vert {domain.e_vert}")
    if not math.isfinite(znw[0]) or abs(znw[0] - 1.0) > 1e-5 or abs(znw[-1]) > 1e-5:
        raise ValueError("Solver ZNW must span [1.0, 0.0]")
    for idx in range(1, len(znw)):
        if not math.isfinite(znw[idx]) or znw[idx] >= znw[idx - 1]:
            raise ValueError(f"Non-monotonic solver ZNW at index {idx}")
    if not all(
        math.isclose(a, b, rel_tol=0.0, abs_tol=2.0e-6)
        for a, b in zip(znw, auto_eta_faces(domain), strict=True)
    ):
        raise ValueError("Solver ZNW disagrees with configured WRF automatic levels")


def _check_column_bounds(midpoints_agl_m: Sequence[float], domain: DomainProfile) -> None:
    if domain.dzbot_m < MIN_STABLE_DZBOT_M:
        raise ValueError(f"dzbot_m ({domain.dzbot_m}) must be >= {MIN_STABLE_DZBOT_M} m")
    if domain.max_dz_m > MAX_SAFE_REFERENCE_DZ_M:
        raise ValueError(
            f"max_dz_m ({domain.max_dz_m}) must be <= {MAX_SAFE_REFERENCE_DZ_M} m "
            "to leave room for thermal expansion below the consumer profile-gap limit"
        )
    if domain.exported_levels_k > MAX_EXPORTED_LEVELS or domain.exported_levels_k >= domain.e_vert:
        raise ValueError("exported_levels_k exceeds contract or computational level count")
    if len(midpoints_agl_m) < domain.exported_levels_k:
        raise ValueError("Column has fewer levels than exported_levels_k")
    if not math.isfinite(midpoints_agl_m[0]) or midpoints_agl_m[0] <= CONSUMER_MIN_LEVEL_AGL_M:
        raise ValueError(
            f"Lowest mass level midpoint ({midpoints_agl_m[0]:.1f} m) must exceed consumer "
            f"minimumLevelAglM ({CONSUMER_MIN_LEVEL_AGL_M:.1f} m AGL)"
        )
    if midpoints_agl_m[0] > domain.max_dz_m:
        raise ValueError("Lowest mass level midpoint exceeds max_dz_m")


def validate_vertical_column(
    midpoints_agl_m: Sequence[float],
    domain: DomainProfile,
) -> int:
    """Verify monotonic levels, >20 m AGL usability, max_dz, and usable BL level count."""
    _check_column_bounds(midpoints_agl_m, domain)
    bl_count = 1 if CONSUMER_MIN_LEVEL_AGL_M < midpoints_agl_m[0] <= BL_CEILING_AGL_M else 0
    for idx in range(1, domain.exported_levels_k):
        if not math.isfinite(midpoints_agl_m[idx]):
            raise ValueError(f"Non-finite vertical level at index {idx}")
        dz = midpoints_agl_m[idx] - midpoints_agl_m[idx - 1]
        if dz <= 0.0:
            raise ValueError(f"Non-monotonic vertical levels at index {idx}")
        if dz > domain.max_dz_m + 1e-6:
            raise ValueError(f"Vertical gap {dz:.1f} m at level {idx} exceeds {domain.max_dz_m} m")
        if CONSUMER_MIN_LEVEL_AGL_M < midpoints_agl_m[idx] <= BL_CEILING_AGL_M:
            bl_count += 1
    if bl_count < domain.min_bl_levels_2km_agl:
        raise ValueError(
            f"Only {bl_count} usable mass levels in (20, 2000] m AGL; "
            f"requires >= {domain.min_bl_levels_2km_agl}"
        )
    return bl_count


def model_column_spacing(
    agl_m: npt.NDArray[np.float64],
) -> tuple[npt.NDArray[np.float64], int]:
    """Return terrain-to-first and successive mass gaps, and minimum usable BL level count."""
    min_lowest = float(np.min(agl_m[0]))
    if min_lowest <= CONSUMER_MIN_LEVEL_AGL_M:
        raise ValueError(
            f"Lowest mass midpoint ({min_lowest:.2f} m AGL) <= "
            f"{CONSUMER_MIN_LEVEL_AGL_M} m consumer minimumLevelAglM"
        )
    gaps_m = np.empty_like(agl_m)
    gaps_m[0] = agl_m[0]
    np.subtract(agl_m[1:], agl_m[:-1], out=gaps_m[1:])
    bl_levels = (agl_m > CONSUMER_MIN_LEVEL_AGL_M) & (agl_m <= BL_CEILING_AGL_M)
    return gaps_m, int(np.min(np.count_nonzero(bl_levels, axis=0)))


def _validate_column_geometry(
    min_gap: float, max_gap: float, min_bl: int, d: DomainProfile
) -> None:
    if not (math.isfinite(min_gap) and math.isfinite(max_gap)):
        raise ValueError("Non-finite column gap")
    if min_gap <= 0.0 or max_gap > CONSUMER_MAX_PROFILE_GAP_M:
        raise ValueError(
            f"Exported column gap [{min_gap:.1f}, {max_gap:.1f}] m "
            f"violates (0, {CONSUMER_MAX_PROFILE_GAP_M}] m"
        )
    if min_bl < d.min_bl_levels_2km_agl:
        raise ValueError(f"Exported usable BL count {min_bl} < {d.min_bl_levels_2km_agl}")


def validate_wrfinput_columns(
    ph: npt.NDArray[np.float32],
    phb: npt.NDArray[np.float32],
    hgt_m: npt.NDArray[np.float32],
    domain: DomainProfile,
) -> None:
    """Gate solver-initialized columns over the published interior before integration."""
    trim = domain.bdy_trim_cells
    phi = np.add(ph[:, trim:-trim, trim:-trim], phb[:, trim:-trim, trim:-trim], dtype=np.float64)
    agl = (phi[1:] + phi[:-1]) * (0.5 / G0_MPS2) - hgt_m[trim:-trim, trim:-trim]
    gaps_m, min_bl = model_column_spacing(agl)
    _validate_column_geometry(float(np.min(gaps_m)), float(np.max(gaps_m)), min_bl, domain)


def validate_frame_columns(frame: NormalizedWrfFrame, domain: DomainProfile) -> float:
    """Verify exported column geometry and record the output-frame CFL proxy."""
    _validate_column_geometry(frame.min_dz_m, frame.max_dz_m, frame.min_bl_levels_2km, domain)
    if not math.isfinite(frame.max_cfl) or frame.max_cfl < 0.0:
        raise ValueError(f"Invalid exported frame max_cfl {frame.max_cfl:.4f}")
    return round(frame.max_cfl, 5)


__all__ = [
    "BL_CEILING_AGL_M",
    "CONSUMER_MAX_PROFILE_GAP_M",
    "CONSUMER_MIN_LEVEL_AGL_M",
    "MAX_SAFE_REFERENCE_DZ_M",
    "MIN_STABLE_DZBOT_M",
    "auto_eta_faces",
    "estimate_auto_levels_agl_m",
    "model_column_spacing",
    "validate_frame_columns",
    "validate_solver_znw",
    "validate_vertical_column",
    "validate_wrfinput_columns",
]
