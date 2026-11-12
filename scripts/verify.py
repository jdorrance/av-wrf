"""Run the repository's complete verification suite."""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REQUIRED_EXTERNAL_TOOLS: tuple[str, ...] = (
    "tach",
    "lint-imports",
    "ruff",
    "mypy",
    "pytest",
)


def _run_step(label: str, cmd: list[str]) -> None:
    print(f"==> {label}: {' '.join(cmd)}")
    proc = subprocess.run(cmd, cwd=ROOT, check=False)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)

    missing = [tool for tool in REQUIRED_EXTERNAL_TOOLS if shutil.which(tool) is None]
    if missing:
        print(
            f"FAIL verify: missing required tools: {', '.join(missing)}. "
            "Run `uv sync --locked && uv run verify`."
        )
        return 1

    py = sys.executable
    _run_step("repo-boundaries-and-locks", [py, "scripts/check_repo.py"])
    _run_step("reference-and-markdown-links", [py, "scripts/generate_reference.py", "--check"])
    _run_step("tach-module-dag", ["tach", "check"])
    _run_step("import-linter", ["lint-imports"])
    _run_step("ruff-lint", ["ruff", "check"])
    _run_step("ruff-format", ["ruff", "format", "--check"])
    _run_step("mypy-strict", ["mypy"])
    _run_step("pytest", ["pytest", "-m", "not container"])
    if shutil.which("git") and (ROOT / ".git").exists():
        _run_step("git-whitespace-check", ["git", "diff", "--check"])
    print("verify: ALL GATES PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
