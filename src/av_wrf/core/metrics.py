"""Pure surface pressure tendency for spin-up diagnostics."""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np


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
