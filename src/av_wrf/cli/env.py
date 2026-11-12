"""Sole module in av-wrf permitted to read os.environ (1:1 with .env.example)."""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class RuntimeEnv:
    store_uri: str
    stage_store_uri: str
    mpi_ranks: int
    mpi_launcher: tuple[str, ...]
    webhook_url: str
    build_manifest_path: str
    oci_digest: str
    solver_prefix: str


def load_runtime_env(environ: Mapping[str, str] | None = None) -> RuntimeEnv:
    """Parse process environment once into a validated frozen RuntimeEnv."""
    env = os.environ if environ is None else environ
    ranks = int(env.get("AV_WRF_MPI_RANKS", "4"))
    if ranks < 1:
        raise ValueError("AV_WRF_MPI_RANKS must be >= 1")
    default_launcher = "mpirun --allow-run-as-root -np" if os.geteuid() == 0 else "mpirun -np"
    launcher = tuple(shlex.split(env.get("AV_WRF_MPI_LAUNCHER", default_launcher)))
    if not launcher or not launcher[0]:
        raise ValueError("AV_WRF_MPI_LAUNCHER must include the launcher and rank flag")
    store_uri = env.get("AV_WRF_STORE_URI", "").strip()
    stage_uri = env.get("AV_WRF_STAGE_STORE_URI", "").strip() or store_uri
    webhook_url = env.get("AV_WRF_WEBHOOK_URL", "").strip()
    manifest_path = env.get("AV_WRF_BUILD_MANIFEST", "/opt/av-wrf/build-manifest.json").strip()
    oci_digest = env.get("AV_WRF_OCI_DIGEST", "").strip()
    solver_prefix = env.get("AV_WRF_SOLVER_PREFIX", "/opt/av-wrf").strip() or "/opt/av-wrf"
    return RuntimeEnv(
        store_uri=store_uri,
        stage_store_uri=stage_uri,
        mpi_ranks=ranks,
        mpi_launcher=launcher,
        webhook_url=webhook_url,
        build_manifest_path=manifest_path,
        oci_digest=oci_digest,
        solver_prefix=solver_prefix,
    )
