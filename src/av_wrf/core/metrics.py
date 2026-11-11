"""Pure verification metrics and scheme-independent soaring boundary-layer column proxies.

Assumptions:
1. Scheme-independent `zi`: WRF PBL schemes diagnose internal `PBLH` differently (YSU uses
   bulk `Rib = 0.0` in unstable conditions; MYNN blends TKE + theta_l). Scoring both radiosondes
   (`UGEE`) and model columns with the Holzworth (1964) / Stull (1988) `+0.5 K` virtual-theta
   parcel excess avoids scheme-specific `PBLH` bias during Phase 5 physics ablations.
2. Boundary-layer shear: vector wind shear is evaluated from the lowest level (`10 m` / surface)
   to `zi_agl_m` (matching `windy-av-layer` `blWindShearMps` and Dr. Jack RASP `bsratio`), never
   to the top of a stratospheric radiosonde; note in deep valleys thermally driven slope flow can
   decouple from ridge-level flow aloft (`meteo/briefing/docs/derive.md`).
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def interpolate_at_height_agl(
    heights_agl_m: Sequence[float],
    values: Sequence[float],
    target_agl_m: float,
) -> float:
    """Linearly interpolate a 1D column quantity at target_agl_m (clamped to column bounds)."""
    if len(heights_agl_m) < 1 or len(heights_agl_m) != len(values):
        raise ValueError("heights_agl_m and values must be non-empty with matching lengths")
    if target_agl_m <= heights_agl_m[0]:
        return values[0]
    if target_agl_m >= heights_agl_m[-1]:
        return values[-1]
    for idx in range(1, len(heights_agl_m)):
        z0, z1 = heights_agl_m[idx - 1], heights_agl_m[idx]
        if z1 <= z0:
            raise ValueError("heights_agl_m must strictly increase")
        if target_agl_m <= z1:
            frac = (target_agl_m - z0) / (z1 - z0)
            return values[idx - 1] + frac * (values[idx] - values[idx - 1])
    return values[-1]


def mixed_layer_depth_agl_m(
    heights_agl_m: Sequence[float],
    theta_v_k: Sequence[float],
    excess_k: float = 0.5,
) -> float:
    """Estimate convective mixed-layer top zi where theta_v exceeds surface + excess_k."""
    if len(heights_agl_m) < 2 or len(heights_agl_m) != len(theta_v_k):
        raise ValueError("heights_agl_m and theta_v_k must match with >= 2 levels")
    threshold = theta_v_k[0] + excess_k
    for idx in range(1, len(heights_agl_m)):
        if theta_v_k[idx] >= threshold:
            t0, t1 = theta_v_k[idx - 1], theta_v_k[idx]
            z0, z1 = heights_agl_m[idx - 1], heights_agl_m[idx]
            frac = 0.0 if abs(t1 - t0) < 1e-9 else (threshold - t0) / (t1 - t0)
            return z0 + min(max(frac, 0.0), 1.0) * (z1 - z0)
    return heights_agl_m[-1]


def capping_inversion_strength_k(
    heights_agl_m: Sequence[float],
    theta_v_k: Sequence[float],
    zi_agl_m: float,
    band_m: float = 300.0,
) -> float:
    """Measure virtual potential temperature jump across [zi_agl_m, zi_agl_m + band_m]."""
    if len(heights_agl_m) != len(theta_v_k):
        raise ValueError("heights_agl_m and theta_v_k lengths must match")
    in_band = [
        theta_v_k[idx]
        for idx in range(len(heights_agl_m))
        if zi_agl_m <= heights_agl_m[idx] <= zi_agl_m + band_m
    ]
    return (max(in_band) - min(in_band)) if len(in_band) >= 2 else 0.0


def vector_wind_shear_mps(
    u_bottom_mps: float,
    v_bottom_mps: float,
    u_top_mps: float,
    v_top_mps: float,
) -> float:
    """Compute bulk vector wind difference magnitude across a layer."""
    return math.hypot(u_top_mps - u_bottom_mps, v_top_mps - v_bottom_mps)


def pressure_tendency_abs_hpa_hr(
    prev_psfc_pa: Sequence[float],
    curr_psfc_pa: Sequence[float],
    dt_hours: float,
) -> float:
    """Compute domain-mean |dps/dt| in hPa/h for spin-up diagnostics."""
    n = len(prev_psfc_pa)
    if dt_hours <= 0.0 or n == 0 or n != len(curr_psfc_pa):
        raise ValueError("Matching non-empty surface pressure arrays and dt_hours > 0 required")
    mean_pa = sum(abs(curr_psfc_pa[i] - prev_psfc_pa[i]) for i in range(n)) / n
    return (mean_pa / 100.0) / dt_hours
