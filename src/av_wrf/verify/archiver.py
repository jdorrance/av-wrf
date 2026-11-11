"""Prospective archive key layout for rolling forcing and consumer baselines."""

from __future__ import annotations


def archive_partition_prefix(model_id: str, cycle_utc: str) -> str:
    """Return immutable archive prefix isolated from live upstream/{model}/ stores."""
    if not model_id or not cycle_utc:
        raise ValueError("model_id and cycle_utc must be non-empty")
    return f"archive/prospective/{model_id}/{cycle_utc}"
