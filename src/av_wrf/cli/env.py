"""Sole module in av-wrf permitted to read os.environ (1:1 with .env.example)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeEnv:
    store_uri: str
    mpi_ranks: int
    webhook_url: str
    build_manifest_path: str
    oci_digest: str


def load_runtime_env(environ: Mapping[str, str] | None = None) -> RuntimeEnv:
    """Parse process environment once into a validated frozen RuntimeEnv."""
    env = os.environ if environ is None else environ
    ranks = int(env.get("AV_WRF_MPI_RANKS", "4"))
    if ranks < 1:
        raise ValueError("AV_WRF_MPI_RANKS must be >= 1")
    store_uri = env.get("AV_WRF_STORE_URI", "file:///tmp/av-wrf-store").strip()
    if not store_uri:
        raise ValueError("AV_WRF_STORE_URI must be non-empty")
    webhook_url = env.get("AV_WRF_WEBHOOK_URL", "").strip()
    manifest_path = env.get("AV_WRF_BUILD_MANIFEST", "/opt/av-wrf/build-manifest.json").strip()
    oci_digest = env.get("AV_WRF_OCI_DIGEST", "").strip()
    return RuntimeEnv(
        store_uri=store_uri,
        mpi_ranks=ranks,
        webhook_url=webhook_url,
        build_manifest_path=manifest_path,
        oci_digest=oci_digest,
    )
