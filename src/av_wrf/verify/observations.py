"""Typed observation records for RAOB, METAR, mountain AWS, and IGC climb envelopes."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SoundingObservation:
    station_id: str
    valid_time_utc: str
    heights_agl_m: tuple[float, ...]
    theta_v_k: tuple[float, ...]
    u_mps: tuple[float, ...]
    v_mps: tuple[float, ...]
