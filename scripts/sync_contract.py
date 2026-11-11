"""Contract sync from windy-av-layer and SHA-256 lock verifier for manifest.lock."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

LOCKED_PREFIXES: tuple[str, ...] = (
    "vendor/producer-contract",
    "docker/patches",
    "tests/oracle",
)
CONSUMER_REL_SCHEMA = "packages/contracts/schemas/wrf-zarr-v3.schema.json"
VENDOR_REL_SCHEMA = "vendor/producer-contract/wrf-zarr-v3.schema.json"


def compute_locked_digests(repo_root: Path) -> dict[str, str]:
    """Compute sorted relative-path SHA-256 digests across all locked directories."""
    digests: dict[str, str] = {}
    for prefix in LOCKED_PREFIXES:
        base = repo_root / prefix
        if not base.exists():
            continue
        for file_path in sorted(base.rglob("*")):
            if (
                not file_path.is_file()
                or "__pycache__" in file_path.parts
                or file_path.suffix == ".pyc"
            ):
                continue
            rel = file_path.relative_to(repo_root).as_posix()
            digests[rel] = hashlib.sha256(file_path.read_bytes()).hexdigest()
    return digests


def write_manifest_lock(repo_root: Path) -> Path:
    lock_path = repo_root / "manifest.lock"
    payload = {
        "lock_version": 1,
        "locked_prefixes": list(LOCKED_PREFIXES),
        "files": compute_locked_digests(repo_root),
    }
    lock_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return lock_path


def sync_from_consumer(repo_root: Path, consumer_root: Path) -> None:
    """Copy canonical wrf-zarr-v3 schema from windy-av-layer and refresh manifest.lock."""
    src = consumer_root / CONSUMER_REL_SCHEMA
    if not src.is_file():
        raise FileNotFoundError(
            f"Consumer contract not yet present at {src}; vendor contract remains provisional"
        )
    shutil.copy2(src, repo_root / VENDOR_REL_SCHEMA)
    write_manifest_lock(repo_root)


def verify_manifest_lock(repo_root: Path) -> list[str]:
    lock_path = repo_root / "manifest.lock"
    if not lock_path.is_file():
        return ["manifest.lock is missing. FIX: Restore manifest.lock from git."]
    recorded = json.loads(lock_path.read_text(encoding="utf-8")).get("files", {})
    actual = compute_locked_digests(repo_root)
    errors: list[str] = []
    for rel in sorted(set(recorded) | set(actual)):
        if rel not in recorded:
            errors.append(
                f"{rel}: untracked file in locked prefix. "
                "FIX: Remove unauthorized file or perform human-approved contract sync."
            )
        elif rel not in actual:
            errors.append(f"{rel}: locked file was deleted. FIX: Restore {rel}.")
        elif recorded[rel] != actual[rel]:
            errors.append(
                f"{rel}: SHA-256 mismatch against manifest.lock. "
                "FIX: Revert unauthorized edits to vendored contracts/oracle."
            )
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync or verify manifest.lock digests.")
    parser.add_argument(
        "--from-consumer",
        type=Path,
        help="Copy canonical schema from windy-av-layer checkout and update manifest.lock",
    )
    parser.add_argument(
        "--allow-lock-rewrite",
        action="store_true",
        help="Explicit human flag to recompute manifest.lock digests",
    )
    parser.add_argument("--verify", action="store_true", help="Check manifest.lock digests")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    if args.from_consumer is not None:
        sync_from_consumer(root, args.from_consumer)
        print(f"Synced {VENDOR_REL_SCHEMA} from {args.from_consumer}")
        return 0
    if args.allow_lock_rewrite:
        out = write_manifest_lock(root)
        print(f"Updated {out.relative_to(root)}")
        return 0
    failures = verify_manifest_lock(root)
    if failures:
        for msg in failures:
            print(f"FAIL {msg}")
        return 1
    print("manifest.lock verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
