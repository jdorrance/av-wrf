"""Container binary manifest verification and synchronized checkpoint guards."""

from __future__ import annotations

from av_wrf.contracts import BuildManifest, CheckpointManifest, RunLock


def verify_checkpoint_compatibility(lock: RunLock, checkpoint: CheckpointManifest) -> None:
    """Ensure interrupted-run recovery matches OCI image digest and MPI rank layout."""
    if checkpoint.oci_digest != lock.build_manifest.oci_digest:
        raise ValueError("Checkpoint OCI digest mismatch; refusing cross-binary wrfrst resume")
    if checkpoint.mpi_ranks != lock.mpi_ranks:
        raise ValueError("Checkpoint MPI rank count mismatch; refusing incompatible resume")
    if not checkpoint.wrfrst_sha256s:
        raise ValueError("Checkpoint manifest contains no wrfrst file digests")


def parse_build_manifest(raw: dict[str, object], oci_digest: str) -> BuildManifest:
    """Validate container /opt/av-wrf/build-manifest.json into a frozen BuildManifest."""
    return BuildManifest(
        oci_digest=oci_digest,
        wrf_git_sha=str(raw["wrf_sha"]),
        wps_git_sha=str(raw["wps_sha"]),
        patch_sha256s=(),
        compiler_version=str(raw.get("compiler", "gfortran")),
        mpi_version=str(raw.get("mpi", "openmpi")),
    )
