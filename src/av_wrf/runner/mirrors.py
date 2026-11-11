"""Bounded mirror acquisition returning fail-closed VerifiedForcingBundle records."""

from __future__ import annotations

from dataclasses import dataclass

from av_wrf.contracts import ForcingProfile
from av_wrf.core import verify_forcing_completeness


@dataclass(frozen=True)
class VerifiedForcingBundle:
    """Proof token produced only after all required forcing keys pass completeness checks."""

    profile_id: str
    cycle_utc: str
    verified_keys: tuple[str, ...]


def build_verified_bundle(
    profile: ForcingProfile,
    cycle_utc: str,
    required_keys: frozenset[str],
    fetched_keys: frozenset[str],
    nonfinite_keys: frozenset[str] = frozenset(),
) -> VerifiedForcingBundle:
    """Validate fetched forcing inventory and return an immutable bundle token."""
    verify_forcing_completeness(required_keys, fetched_keys, nonfinite_keys)
    return VerifiedForcingBundle(
        profile_id=profile.profile_id,
        cycle_utc=cycle_utc,
        verified_keys=tuple(sorted(fetched_keys)),
    )
