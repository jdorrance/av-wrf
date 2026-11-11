"""Mature-lead NWP and soaring-proxy scorecard evaluation."""

from __future__ import annotations

from dataclasses import dataclass

from av_wrf.core import (
    capping_inversion_strength_k,
    interpolate_at_height_agl,
    mixed_layer_depth_agl_m,
    vector_wind_shear_mps,
)
from av_wrf.verify.observations import SoundingObservation


@dataclass(frozen=True)
class SoaringColumnSummary:
    zi_agl_m: float
    capping_delta_theta_v_k: float
    bulk_shear_mps: float


def evaluate_column_soaring_proxies(obs: SoundingObservation) -> SoaringColumnSummary:
    """Compute zi, capping inversion strength, and [z_0, zi] BL vector wind shear on a column."""
    zi = mixed_layer_depth_agl_m(obs.heights_agl_m, obs.theta_v_k)
    cap = capping_inversion_strength_k(obs.heights_agl_m, obs.theta_v_k, zi)
    u_zi = interpolate_at_height_agl(obs.heights_agl_m, obs.u_mps, zi)
    v_zi = interpolate_at_height_agl(obs.heights_agl_m, obs.v_mps, zi)
    shear = vector_wind_shear_mps(obs.u_mps[0], obs.v_mps[0], u_zi, v_zi)
    return SoaringColumnSummary(zi_agl_m=zi, capping_delta_theta_v_k=cap, bulk_shear_mps=shear)
