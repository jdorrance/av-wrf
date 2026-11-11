"""Fail-closed forcing completeness verification and Noah-MP soil interpolation.

Assumptions & Land-State Cold-Start Limits:
1. Soil depth centers: Noah-MP uses 4 layers (`0-10, 10-40, 40-100, 100-200 cm` -> centers
   `5, 25, 70, 150 cm`), matching GFS 0.25° Noah centers 1:1 while requiring linear interpolation
   from ECMWF IFS 0.1° HTESSEL centers (`3.5, 17.5, 64, 194.5 cm`), matching `real.exe` Sec. 5.2.6.
2. Soil-texture hydraulic mismatch: interpolating raw volumetric `SMOIS` (`m3/m3`) across coarse
   forcing soil classes vs 30 arc-sec WRF soil classes can shift relative soil saturation and Bowen
   ratio (`HFX / LH`); Phase 1 absorbs fast surface adjustment via a 6 h cold-start spin-up, and
   Phase 5 benchmarks warm `wrfinput` Noah-MP land-state cycling, `IMS 1 km` snow, and Lake Sevan
   (`1,900 m` MSL) `OSTIA` skin temperatures.

References:
- Noah-MP 4-layer soil state: https://github.com/NCAR/noahmp
- WRF v4 Technical Note (Sec. 5.2.6):
  https://www2.mmm.ucar.edu/wrf/users/docs/technote/v4_technote.pdf
"""

from __future__ import annotations

from collections.abc import Sequence

NOAH_MP_SOIL_CENTERS_CM: tuple[float, float, float, float] = (5.0, 25.0, 70.0, 150.0)


def verify_forcing_completeness(
    required_keys: frozenset[str],
    present_keys: frozenset[str],
    nonfinite_keys: frozenset[str] = frozenset(),
) -> None:
    """Fail closed if any required forcing message/level is absent or contains NaN/Inf."""
    missing = sorted(required_keys - present_keys)
    if missing:
        raise ValueError(f"Incomplete forcing bundle; missing keys: {', '.join(missing[:8])}")
    corrupt = sorted(required_keys & nonfinite_keys)
    if corrupt:
        raise ValueError(f"Forcing bundle contains non-finite values: {', '.join(corrupt[:8])}")


def interpolate_soil_profile(
    src_depths_cm: Sequence[float],
    src_values: Sequence[float],
    dst_depths_cm: Sequence[float] = NOAH_MP_SOIL_CENTERS_CM,
    *,
    lower_bound: float,
    upper_bound: float,
) -> tuple[float, ...]:
    """Linearly interpolate forcing soil layers onto Noah-MP centers within physical bounds."""
    if len(src_depths_cm) < 2 or len(src_depths_cm) != len(src_values):
        raise ValueError("Source soil depths and values must match and contain >= 2 layers")
    if lower_bound > upper_bound:
        raise ValueError("lower_bound must not exceed upper_bound")
    for idx in range(1, len(src_depths_cm)):
        if src_depths_cm[idx] <= src_depths_cm[idx - 1]:
            raise ValueError("Source soil depths must strictly increase")

    result: list[float] = []
    for target in dst_depths_cm:
        if target <= src_depths_cm[0]:
            val = src_values[0]
        elif target >= src_depths_cm[-1]:
            val = src_values[-1]
        else:
            val = src_values[-1]
            for idx in range(1, len(src_depths_cm)):
                if target <= src_depths_cm[idx]:
                    d0, d1 = src_depths_cm[idx - 1], src_depths_cm[idx]
                    weight = (target - d0) / (d1 - d0)
                    val = src_values[idx - 1] + weight * (src_values[idx] - src_values[idx - 1])
                    break
        result.append(min(max(val, lower_bound), upper_bound))
    return tuple(result)
