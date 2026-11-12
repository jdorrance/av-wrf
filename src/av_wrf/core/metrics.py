"""Pure verification metrics and scheme-independent soaring boundary-layer column proxies.

Assumptions:
1. Scheme-independent `zi`: WRF PBL schemes diagnose internal `PBLH` differently (YSU uses
   bulk `Rib = 0.0` in unstable conditions; MYNN blends TKE + theta_l). Scoring both radiosondes
   (`UGEE`) and model columns with the Holzworth (1964) / Stull (1988) `+0.5 K` virtual-theta
   parcel excess avoids scheme-specific `PBLH` bias during Phase 5 physics ablations.
2. Interpolated capping inversion `[zi, zi + band_m]`: linearly interpolates `theta_v` at exact
   band endpoints `zi` and `zi + band_m` so coarse upper-BL levels never artificially return `0.0`
   when fewer than 2 discrete nodes fall inside the 300 m window, and fails closed if the sounding
   terminates below `zi + band_m`.
3. Boundary-layer shear: vector wind shear is evaluated from the lowest level (`10 m` / surface)
   to `zi_agl_m` (matching `windy-av-layer` `blWindShearMps` and Dr. Jack RASP `bsratio`).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from itertools import pairwise

import numpy as np


def _validate_profile(heights_agl_m: Sequence[float], values: Sequence[float]) -> None:
    if not heights_agl_m or len(heights_agl_m) != len(values):
        raise ValueError("Heights and values must be nonempty with matching lengths")
    if not all(map(math.isfinite, heights_agl_m)) or not all(map(math.isfinite, values)):
        raise ValueError("Heights and values must be finite")
    if any(z1 <= z0 for z0, z1 in pairwise(heights_agl_m)):
        raise ValueError("heights_agl_m must strictly increase")


def interpolate_at_height_agl(
    heights_agl_m: Sequence[float],
    values: Sequence[float],
    target_agl_m: float,
) -> float:
    """Linearly interpolate a 1D column quantity at target_agl_m (clamped to column bounds)."""
    _validate_profile(heights_agl_m, values)
    if not math.isfinite(target_agl_m):
        raise ValueError("Target height must be finite")
    if target_agl_m <= heights_agl_m[0]:
        return values[0]
    if target_agl_m >= heights_agl_m[-1]:
        return values[-1]
    for idx in range(1, len(heights_agl_m)):
        z0, z1 = heights_agl_m[idx - 1], heights_agl_m[idx]
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
    _validate_profile(heights_agl_m, theta_v_k)
    if len(heights_agl_m) < 2 or not math.isfinite(excess_k) or excess_k <= 0.0:
        raise ValueError("At least two levels and a positive finite parcel excess are required")
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
    """Measure positive virtual potential temperature jump across interpolated [zi, zi + band_m]."""
    if not math.isfinite(band_m) or band_m <= 0.0 or len(heights_agl_m) < 2:
        raise ValueError("A positive finite inversion band and at least two levels are required")
    top_agl = zi_agl_m + band_m
    if not heights_agl_m[0] <= zi_agl_m < top_agl <= heights_agl_m[-1]:
        raise ValueError(
            f"Column does not cover inversion band [zi={zi_agl_m:.1f} m, {top_agl:.1f} m]"
        )
    tv_zi = interpolate_at_height_agl(heights_agl_m, theta_v_k, zi_agl_m)
    tv_top = interpolate_at_height_agl(heights_agl_m, theta_v_k, top_agl)
    best_jump, running_min = 0.0, tv_zi
    for z_m, tv_k in zip(heights_agl_m, theta_v_k, strict=True):
        if zi_agl_m < z_m < top_agl:
            best_jump = max(best_jump, tv_k - running_min)
            running_min = min(running_min, tv_k)
    return max(best_jump, tv_top - running_min)


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
    delta_pa = np.subtract(curr_psfc_pa, prev_psfc_pa, dtype=np.float64)
    np.abs(delta_pa, out=delta_pa)
    mean_pa = float(np.mean(delta_pa))
    return (mean_pa / 100.0) / dt_hours
