"""Vertical auto_levels_opt=2 + hybrid_opt=2 column estimation and high-terrain BL checks.

Assumptions & High-Mountain Coordinate Math (Skamarock et al. 2019 NCAR/TN-556+STR):
1. `real.exe` (`auto_levels_opt=2`) stretches full levels `znw` (`eta in [1, 0]`) in dry
   log-pressure space using reference sea-level `p0 = 100,000 Pa`, `T0 = 290 K`, `A = 50 K`,
   and `T_min = 200 K` (Sec. 5.2.2), transitioning from `dzstretch_s` to `dzstretch_u` by
   `0.5 * max_dz` and capping at `max_dz` down to `p_top_pa`.
2. Over elevated terrain (`terrain_m > 0`), `hybrid_opt = 2` (`etac`, Eqs. 2.2-2.6 & 5.3-5.4)
   evaluates `p_d(eta) = B(eta)*(p_s - p_t) + [eta - B(eta)]*(p0 - p_t) + p_t` with cubic
   `B(eta)` (`B(1)=1, B'(1)=1, B(etac)=0, B'(etac)=0`). Because `B'(eta)` peaks above `1.0`
   at mid-levels (`1.68` for `etac=0.2`), layers compress over mountains (`4,090 m` Aragats,
   `5,137 m` Ararat) and can fold (`dp_d <= 0`) if `etac` is set too high; this module checks
   `dp_d > 0` before any solver run.
3. Consumer profile-gap ceiling: `windy-av-layer` (`column.ts:159`) marks an entire column
   `unavailable` if ANY adjacent exported level gap exceeds `maximumProfileGapM = 1,000 m`.
   Because summer heatwaves expand hydrostatic thickness by ~6-8% relative to the `290 K`
   reference state, `MAX_SAFE_REFERENCE_DZ_M = 900.0` enforces >= 10% thermal headroom.
4. Consumer near-ground screening: `column.ts` retains `k=0` (`~15-17 m` AGL), whereas
   `native.ts` skips levels `<= minimumLevelAglM = 20 m` and starts at `k=1` (`~45-52 m` AGL).

References:
- WRF v4 Technical Note (Ch. 2 & 5):
  https://www2.mmm.ucar.edu/wrf/users/docs/technote/v4_technote.pdf
- WRF auto_levels_opt = 2:
  https://www2.mmm.ucar.edu/wrf/site/users_guide/real_initialization.html
- Surface-layer dzbot >= 25 m:
  https://forum.mmm.ucar.edu/threads/increase-eta-level-and-decrease-dzbot.8325/
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from av_wrf.contracts import MAX_EXPORTED_LEVELS, DomainProfile
from av_wrf.core.thermo import G0_MPS2, P0_PA, R_DRY_J_KG_K

MIN_STABLE_DZBOT_M = 25.0
MAX_SAFE_REFERENCE_DZ_M = 900.0
REF_T0_K = 290.0
REF_LAPSE_A_K = 50.0
REF_TMIN_K = 200.0


def _ref_temp_k(p_pa: float) -> float:
    return max(REF_TMIN_K, REF_T0_K + REF_LAPSE_A_K * math.log(max(1.0, p_pa) / P0_PA))


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
    n_mass = domain.e_vert - 1
    half_max = 0.5 * domain.max_dz_m
    dz = domain.dzbot_m
    dlnp_steps: list[float] = []
    p_cur = P0_PA
    for _ in range(n_mass):
        t_k = _ref_temp_k(p_cur)
        step = (G0_MPS2 * dz) / (R_DRY_J_KG_K * t_k)
        dlnp_steps.append(step)
        p_cur *= math.exp(-step)
        if dz < half_max:
            frac = max(0.0, min(1.0, (dz - domain.dzbot_m) / max(1.0, half_max - domain.dzbot_m)))
            stretch = domain.dzstretch_s + frac * (domain.dzstretch_u - domain.dzstretch_s)
        else:
            stretch = domain.dzstretch_u
        dz = min(dz * stretch, domain.max_dz_m)

    target_lnp = math.log(P0_PA / domain.p_top_pa)
    total_lnp = sum(dlnp_steps)
    if total_lnp < target_lnp:
        raise ValueError(
            "Vertical e_vert / stretching fails to reach p_top_pa without exceeding max_dz_m"
        )
    # Fit upper-level steps smoothly to land eta=0 at p_top_pa while preserving dzbot near ground.
    excess = total_lnp - target_lnp
    upper_start = min(domain.exported_levels_k, n_mass - 1)
    upper_sum = sum(dlnp_steps[upper_start:])
    if upper_sum > excess and upper_start < n_mass:
        scale = (upper_sum - excess) / upper_sum
        for idx in range(upper_start, n_mass):
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

    phi_sfc = G0_MPS2 * max(0.0, terrain_m)
    radicand = ((REF_T0_K / REF_LAPSE_A_K) ** 2) - (
        2.0 * phi_sfc / (REF_LAPSE_A_K * R_DRY_J_KG_K)
    )
    if radicand <= 0.0:
        raise ValueError("terrain_m exceeds dry reference atmosphere ceiling")
    ps_ref = P0_PA * math.exp(-(REF_T0_K / REF_LAPSE_A_K) + math.sqrt(radicand))
    if ps_ref <= domain.p_top_pa * 1.2:
        raise ValueError("Reference surface pressure at terrain_m is too close to p_top_pa")

    etas = _reference_eta_faces(domain)
    pt = domain.p_top_pa
    z_face = 0.0
    midpoints: list[float] = []
    p_bot = _hybrid_b(etas[0], etac) * (ps_ref - pt) + (etas[0] - _hybrid_b(etas[0], etac)) * (
        P0_PA - pt
    ) + pt
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


def validate_vertical_column(
    midpoints_agl_m: Sequence[float],
    domain: DomainProfile,
) -> int:
    """Verify monotonic levels, dzbot, thermal-headroom max_dz, and BL count; return BL count."""
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
    if midpoints_agl_m[0] <= 0.0 or midpoints_agl_m[0] > domain.max_dz_m:
        raise ValueError("Lowest mass level midpoint must lie in (0, max_dz_m] AGL")

    bl_count = 1 if midpoints_agl_m[0] <= 2000.0 else 0
    for idx in range(1, domain.exported_levels_k):
        dz = midpoints_agl_m[idx] - midpoints_agl_m[idx - 1]
        if dz <= 0.0:
            raise ValueError(f"Non-monotonic vertical levels at index {idx}")
        if dz > domain.max_dz_m + 1e-6:
            raise ValueError(
                f"Vertical gap {dz:.1f} m at level {idx} exceeds {domain.max_dz_m} m"
            )
        if midpoints_agl_m[idx] <= 2000.0:
            bl_count += 1

    if bl_count < domain.min_bl_levels_2km_agl:
        raise ValueError(
            f"Only {bl_count} mass levels in 0-2000 m AGL; "
            f"requires >= {domain.min_bl_levels_2km_agl}"
        )
    return bl_count
