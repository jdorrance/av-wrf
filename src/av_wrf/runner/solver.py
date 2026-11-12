"""Read installed solver build manifests and verify frozen execution receipts."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from av_wrf.codecs import select_cycle_wrfout_slices
from av_wrf.contracts import BuildManifest, CycleWindow, RunLock, SolverReceipt
from av_wrf.runner.store import ObjectStore, sha256_file, solver_receipt_key


def parse_build_manifest(raw: Mapping[str, object], oci_digest: str) -> BuildManifest:
    """Validate container /opt/av-wrf/build-manifest.json into a frozen BuildManifest."""
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", oci_digest.strip()):
        raise ValueError("BuildManifest requires a pinned OCI SHA-256 digest")
    raw_patches = raw.get("patch_sha256s")
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
    wrf_sha = _git_sha(raw, "wrf_sha")
    wps_sha = _git_sha(raw, "wps_sha")
    jasper_sha = _git_sha(raw, "jasper_sha") if "jasper_sha" in raw else None
    compiler, mpi = raw.get("compiler"), raw.get("mpi")
    if not isinstance(compiler, str) or not compiler or not isinstance(mpi, str) or not mpi:
        raise ValueError("Missing compiler or mpi in build-manifest.json")
    return BuildManifest(
        oci_digest.strip(),
        wrf_sha,
        wps_sha,
        tuple(patches),
        compiler,
        mpi,
        jasper_sha,
    )


def _git_sha(raw: Mapping[str, object], key: str) -> str:
    sha = raw.get(key)
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError(f"Invalid Git commit for {key} in build-manifest.json")
    return sha


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


def _wrfout_files(wrfout_dir: Path) -> list[Path]:
    return sorted(wrfout_dir.glob("wrfout_d01_*"))


def _hash_complete_wrfout(wrfout_dir: Path, window: CycleWindow) -> tuple[tuple[str, str], ...]:
    select_cycle_wrfout_slices(wrfout_dir, window.publish_origin_utc, window.valid_times_utc)
    return tuple((p.name, sha256_file(p)) for p in _wrfout_files(wrfout_dir))


def _verify_wrfout_files(
    wrfout_dir: Path, expected: tuple[tuple[str, str], ...], selected: set[Path] | None
) -> None:
    outputs = _wrfout_files(wrfout_dir)
    if tuple(p.name for p in outputs) != tuple(name for name, _ in expected):
        raise ValueError("wrfout_d01_* inventory does not match SolverReceipt filenames")
    if selected is not None and not selected.issubset(outputs):
        raise ValueError("Selected wrfout files must belong to the SolverReceipt inventory")
    for p, (_, digest) in zip(outputs, expected, strict=True):
        if (selected is None or p in selected) and sha256_file(p) != digest:
            raise ValueError(f"{p.name} does not match its SolverReceipt SHA-256")


def verify_solver_receipt(
    store: ObjectStore,
    lock: RunLock,
    wrfout_dir: Path | None,
    *,
    selected_wrfouts: set[Path] | None = None,
) -> SolverReceipt:
    """Verify receipt provenance, on-disk inventory and hashes of all or selected outputs."""
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
    if not re.fullmatch(r"[0-9a-f]{64}", lock.geog_bundle_sha256) or (
        rec.geog_sha256 != lock.geog_bundle_sha256
    ):
        raise ValueError("SolverReceipt geog_sha256 does not match RunLock.geog_bundle_sha256")
    if not rec.wrfout_sha256s or not rec.forcing_sha256s:
        raise ValueError("SolverReceipt missing forcing or wrfout digests")
    if wrfout_dir is not None:
        _verify_wrfout_files(wrfout_dir, rec.wrfout_sha256s, selected_wrfouts)
    return rec


__all__ = [
    "load_build_manifest",
    "parse_build_manifest",
    "verify_solver_receipt",
]
