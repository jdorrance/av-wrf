"""Container build-manifest checks, checkpoint checks, and SolverReceipt provenance."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from av_wrf.contracts import BuildManifest, CheckpointManifest, RunLock, SolverReceipt
from av_wrf.runner.solver_exec import PROVIDER_VTABLES, run_geogrid, run_wrf_cycle
from av_wrf.runner.store import ObjectStore, sha256_hex, solver_receipt_key


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
        if not isinstance(entry, list) or len(entry) != 2:
            raise ValueError("Invalid patch_sha256s entry in build-manifest.json")
        first, second = entry[0], entry[1]
        if not isinstance(first, str) or not isinstance(second, str):
            raise ValueError("Invalid patch_sha256s entry in build-manifest.json")
        patches.append((first, second))
    wrf_sha, wps_sha = raw.get("wrf_sha"), raw.get("wps_sha")
    if not isinstance(wrf_sha, str) or not wrf_sha or not isinstance(wps_sha, str) or not wps_sha:
        raise ValueError("Missing wrf_sha or wps_sha in build-manifest.json")
    return BuildManifest(
        oci_digest.strip(),
        wrf_sha,
        wps_sha,
        tuple(patches),
        str(raw.get("compiler", "gfortran")),
        str(raw.get("mpi", "openmpi")),
    )


def load_build_manifest(
    manifest_path: Path, oci_digest: str, *, allow_unpinned_dev: bool = False
) -> BuildManifest:
    """Fail closed unless BuildManifest + AV_WRF_OCI_DIGEST exist (or allow_unpinned_dev=True)."""
    if manifest_path.is_file():
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(doc, dict):
            raise ValueError(f"Expected JSON object in {manifest_path}")
        return parse_build_manifest(doc, oci_digest)
    if oci_digest.strip():
        raise FileNotFoundError(f"AV_WRF_OCI_DIGEST set without build manifest at {manifest_path}")
    if not allow_unpinned_dev:
        raise FileNotFoundError(
            f"Missing solver build manifest at {manifest_path}; pass --allow-unpinned-dev for dev"
        )
    return BuildManifest("host-dev-unpinned", "host-unbuilt", "host-unbuilt", (), "host", "host")


def verify_solver_receipt(
    store: ObjectStore, lock: RunLock, wrfout_dir: Path | None
) -> SolverReceipt:
    """Verify stored SolverReceipt binds lock, pinned OCI digest, and wrfout SHA-256 digests."""
    if lock.build_manifest.oci_digest == "host-dev-unpinned":
        raise ValueError("Live publication forbids host-dev-unpinned build manifests")
    raw = store.get_bytes(
        solver_receipt_key(lock.product_id, lock.cycle_partition, lock.attempt_id)
    )
    if raw is None:
        raise FileNotFoundError(
            f"Missing solver_receipt.json for {lock.product_id} {lock.cycle_partition} "
            f"({lock.attempt_id}); run `av-wrf solve` before live `post`/`publish`"
        )
    doc = json.loads(raw.decode("utf-8"))
    if not isinstance(doc, dict):
        raise ValueError("Expected JSON object in solver_receipt.json")
    rec = SolverReceipt.from_doc(doc)
    if (
        rec.product_id != lock.product_id
        or rec.cycle_partition != lock.cycle_partition
        or rec.attempt_id != lock.attempt_id
        or rec.config_sha256 != lock.config_sha256
        or rec.oci_digest != lock.build_manifest.oci_digest
    ):
        raise ValueError("SolverReceipt provenance does not match frozen RunLock")
    if not rec.wrfout_sha256s or not rec.forcing_sha256s:
        raise ValueError("SolverReceipt missing forcing or wrfout digests")
    if wrfout_dir is not None:
        actual = tuple(
            (p.name, sha256_hex(p.read_bytes()))
            for p in sorted(wrfout_dir.glob("wrfout_d01_*"))
            if p.is_file()
        )
        if actual != rec.wrfout_sha256s:
            raise ValueError("wrfout_d01_* files on disk do not match SolverReceipt digests")
    return rec


__all__ = [
    "PROVIDER_VTABLES",
    "load_build_manifest",
    "parse_build_manifest",
    "run_geogrid",
    "run_wrf_cycle",
    "verify_checkpoint_compatibility",
    "verify_solver_receipt",
]
