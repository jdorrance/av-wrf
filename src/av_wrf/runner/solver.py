"""Resolve installed solver assets and verify frozen execution receipts."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
from collections.abc import Mapping
from contextlib import suppress
from pathlib import Path
from tempfile import mkdtemp
from time import perf_counter

from av_wrf.codecs import select_cycle_wrfout_slices
from av_wrf.contracts import BuildManifest, CycleWindow, RunLock, SolverReceipt
from av_wrf.runner.store import (
    ObjectStore,
    _sync_directory,
    json_bytes,
    put_idempotent,
    sha256_file,
    solver_receipt_key,
)

_LOCAL_SOLVER_RECEIPT = ".solver-receipt.json"


def _run_native(command: list[str], cwd: Path, timeout_s: int | None) -> float:
    """Return native stage wall seconds and cancel its process group on timeout."""
    started = perf_counter()
    if timeout_s is None:
        subprocess.run(command, cwd=cwd, check=True)
        return perf_counter() - started
    if timeout_s < 1:
        raise ValueError("Native stage timeout must be positive")
    with subprocess.Popen(command, cwd=cwd, start_new_session=True) as process:
        try:
            code = process.wait(timeout=timeout_s)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            raise
    if code:
        raise subprocess.CalledProcessError(code, command)
    return perf_counter() - started


def _exec_mpi_step(
    attempt_dir: Path,
    cmd: list[str],
    marker: str,
    timeout_s: int | None,
    *,
    reject_cfl: bool = False,
) -> float:
    for rsl in attempt_dir.glob("rsl.*"):
        rsl.unlink()
    seconds = _run_native(cmd, attempt_dir, timeout_s)
    exe_name = Path(cmd[-1]).name
    rsl0 = attempt_dir / "rsl.error.0000"
    if not rsl0.is_file():
        raise RuntimeError(f"{exe_name} produced no rsl.error.0000 log")
    completed = False
    for log in sorted(attempt_dir.glob("rsl.*")) if reject_cfl else (rsl0,):
        with log.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if log == rsl0 and marker in line:
                    completed = True
                    if not reject_cfl:
                        break
                if reject_cfl:
                    lowered = line.lower()
                    if "exceeded cfl" in lowered or "points exceeded v_cfl" in lowered:
                        raise RuntimeError(f"Solver CFL violation detected in {log.name}")
    if not completed:
        raise RuntimeError(f"{exe_name} missing completion marker '{marker}' in rsl.error.0000")
    return seconds


def _resolve_solver_exe(solver_prefix: Path, exe_name: str) -> Path:
    """Resolve a `bin` executable whose target stays within the solver prefix."""
    candidate = (solver_prefix / "bin" / exe_name).resolve()
    if (
        solver_prefix.resolve() not in candidate.parents
        or not candidate.is_file()
        or not os.access(candidate, os.X_OK)
    ):
        raise FileNotFoundError(
            f"Missing executable '{exe_name}' under {solver_prefix / 'bin'} (PATH fallback denied)"
        )
    return candidate


def _link_file(src: Path, dst: Path) -> None:
    if not src.is_file():
        raise FileNotFoundError(f"Missing required solver asset: {src}")
    os.symlink(src.resolve(), dst)


def _link_run_tables(run_tables: Path, attempt_dir: Path) -> None:
    """Link WRF physics tables. Skip the real/wrf/ndown/tc binaries shipped in run/."""
    if not run_tables.is_dir():
        raise FileNotFoundError(f"Missing WRF runtime tables directory: {run_tables}")
    for item in sorted(run_tables.iterdir()):
        if (
            item.is_file()
            and not item.name.startswith("namelist")
            and item.name.removesuffix(".exe") not in {"real", "wrf", "ndown", "tc"}
        ):
            _link_file(item, attempt_dir / item.name)


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


def _verify_runtime_build_manifest(
    solver_prefix: Path, lock: RunLock, runtime: BuildManifest
) -> None:
    installed = load_build_manifest(solver_prefix / "build-manifest.json", runtime.oci_digest)
    if installed != runtime or runtime != lock.build_manifest:
        raise ValueError("Installed solver and runtime build manifests must match the RunLock")


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
    return _verify_solver_receipt_bytes(raw, lock, wrfout_dir, selected_wrfouts)


def _verify_solver_receipt_bytes(
    raw: bytes, lock: RunLock, wrfout_dir: Path | None, selected: set[Path] | None = None
) -> SolverReceipt:
    doc = json.loads(raw)
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
        _verify_wrfout_files(wrfout_dir, rec.wrfout_sha256s, selected)
    return rec


def _recover_solved_attempt(
    store: ObjectStore, key: str, lock: RunLock, attempt: Path
) -> SolverReceipt | None:
    try:
        raw = (attempt / _LOCAL_SOLVER_RECEIPT).read_bytes()
    except FileNotFoundError:
        return None
    receipt = _verify_solver_receipt_bytes(raw, lock, None)
    try:
        _verify_wrfout_files(attempt, receipt.wrfout_sha256s, None)
    except (OSError, ValueError):
        return None
    put_idempotent(store, key, raw)
    return receipt


def _install_solved_attempt(scratch: Path, attempt: Path, receipt: SolverReceipt) -> bytes:
    for name, _ in receipt.wrfout_sha256s:
        with (scratch / name).open("rb") as output:
            os.fsync(output.fileno())
    raw = json_bytes(receipt.to_doc())
    with (scratch / _LOCAL_SOLVER_RECEIPT).open("xb") as journal:
        journal.write(raw)
        journal.flush()
        os.fsync(journal.fileno())
    _sync_directory(scratch)
    if attempt.is_dir():
        archive = Path(mkdtemp(prefix=f".{attempt.name}.previous-", dir=attempt.parent))
        attempt.rename(archive / "work")
        _sync_directory(archive)
    scratch.rename(attempt)
    scratch.parent.rmdir()
    _sync_directory(attempt.parent)
    return raw


__all__ = [
    "load_build_manifest",
    "parse_build_manifest",
    "verify_solver_receipt",
]
