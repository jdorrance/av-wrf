"""Container binary manifest loading/verification and synchronized checkpoint guards."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from av_wrf.contracts import BuildManifest, CheckpointManifest, RunLock


def verify_checkpoint_compatibility(lock: RunLock, checkpoint: CheckpointManifest) -> None:
    """Ensure interrupted-run recovery matches OCI image digest and MPI rank layout."""
    if checkpoint.oci_digest != lock.build_manifest.oci_digest:
        raise ValueError("Checkpoint OCI digest mismatch; refusing cross-binary wrfrst resume")
    if checkpoint.mpi_ranks != lock.mpi_ranks:
        raise ValueError("Checkpoint MPI rank count mismatch; refusing incompatible resume")
    if not checkpoint.wrfrst_sha256s:
        raise ValueError("Checkpoint manifest contains no wrfrst file digests")


def parse_build_manifest(raw: Mapping[str, object], oci_digest: str) -> BuildManifest:
    """Validate container /opt/av-wrf/build-manifest.json into a frozen BuildManifest."""
    if not oci_digest.strip():
        raise ValueError("oci_digest must be non-empty when binding BuildManifest")
    raw_patches = raw.get("patch_sha256s", [])
    if not isinstance(raw_patches, list):
        raise ValueError("Expected list for patch_sha256s in build-manifest.json")
    patches: list[tuple[str, str]] = []
    for entry in raw_patches:
        if (
            not isinstance(entry, list)
            or len(entry) != 2
            or not isinstance(entry[0], str)
            or not isinstance(entry[1], str)
        ):
            raise ValueError("Invalid patch_sha256s entry in build-manifest.json")
        patches.append((entry[0], entry[1]))
    wrf_sha, wps_sha = raw.get("wrf_sha"), raw.get("wps_sha")
    if not isinstance(wrf_sha, str) or not wrf_sha or not isinstance(wps_sha, str) or not wps_sha:
        raise ValueError("Missing wrf_sha or wps_sha in build-manifest.json")
    return BuildManifest(
        oci_digest=oci_digest.strip(),
        wrf_git_sha=wrf_sha,
        wps_git_sha=wps_sha,
        patch_sha256s=tuple(patches),
        compiler_version=str(raw.get("compiler", "gfortran")),
        mpi_version=str(raw.get("mpi", "openmpi")),
    )


def load_build_manifest(
    manifest_path: Path,
    oci_digest: str,
    *,
    allow_unpinned_dev: bool = False,
) -> BuildManifest:
    """Fail closed unless BuildManifest + AV_WRF_OCI_DIGEST exist (or allow_unpinned_dev=True)."""
    if manifest_path.is_file():
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError(f"Expected JSON object in {manifest_path}")
        return parse_build_manifest(doc, oci_digest)
    if oci_digest.strip():
        raise FileNotFoundError(
            f"AV_WRF_OCI_DIGEST is set but build manifest is missing at {manifest_path}"
        )
    if not allow_unpinned_dev:
        raise FileNotFoundError(
            f"Missing solver build manifest at {manifest_path}; pass --allow-unpinned-dev "
            "only for local host dry-runs"
        )
    return BuildManifest(
        oci_digest="host-dev-unpinned",
        wrf_git_sha="host-unbuilt",
        wps_git_sha="host-unbuilt",
        patch_sha256s=(),
        compiler_version="host",
        mpi_version="host",
    )
