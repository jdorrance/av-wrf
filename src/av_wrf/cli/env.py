"""Sole module in av-wrf permitted to read os.environ (1:1 with .env.example)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeEnv:
    store_uri: str
    mpi_ranks: int


def load_runtime_env(environ: Mapping[str, str] | None = None) -> RuntimeEnv:
    """Parse process environment once into a validated frozen RuntimeEnv."""
    env = os.environ if environ is None else environ
    ranks = int(env.get("AV_WRF_MPI_RANKS", "4"))
    if ranks < 1:
        raise ValueError("AV_WRF_MPI_RANKS must be >= 1")
    store_uri = env.get("AV_WRF_STORE_URI", "file:///tmp/av-wrf-store").strip()
    if not store_uri:
        raise ValueError("AV_WRF_STORE_URI must be non-empty")
    return RuntimeEnv(store_uri=store_uri, mpi_ranks=ranks)
