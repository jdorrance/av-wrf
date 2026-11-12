"""Mechanical repository structure, 5-layer DAG, region-fence, budget, and purity linter."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.repo_ast_rules import (  # noqa: E402
    ALLOWED_PACKAGES,
    check_python_file,
    load_public_exports,
)
from scripts.sync_contract import verify_manifest_lock  # noqa: E402

ALLOWED_TOP_LEVEL = frozenset(
    {
        ".dockerignore",
        ".github",
        ".gitignore",
        ".env.example",
        ".pre-commit-config.yaml",
        ".python-version",
        "AGENTS.md",
        "LICENSE",
        "README.md",
        "THIRD_PARTY_NOTICES.txt",
        "config",
        "docker",
        "docs",
        "manifest.lock",
        "pyproject.toml",
        "scripts",
        "src",
        "tach.toml",
        "tests",
        "uv.lock",
        "vendor",
    }
)
IGNORED_NAMES = frozenset(
    {
        ".git",
        ".kilo",
        "__pycache__",
        ".venv",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".import_linter_cache",
        ".hypothesis",
        ".DS_Store",
        ".coverage",
        ".env",
        "build",
        "dist",
        "output",
        "runs",
        "work",
        "scratch",
    }
)
BANNED_FILENAMES = frozenset({"utils.py", "helpers.py", "common.py", "misc.py", "base.py"})
GENERIC_CONFIG_WORDS = frozenset(
    {
        "wrf",
        "pilot",
        "core",
        "dev",
        "test",
        "prod",
        "domain",
        "utc",
        "gmt",
        "etc",
    }
)


def _add_yaml_region_tokens(yml: Path, tokens: set[str]) -> None:
    tokens.add(yml.stem.lower())
    id_keys = {"product_id", "model_id", "profile_id", "domain_profile"}
    for raw_line in yml.read_text(encoding="utf-8").splitlines():
        key, sep, val = raw_line.split("#", 1)[0].strip().partition(":")
        k, v = key.strip(), val.strip().strip("\"'")
        if sep and k in id_keys and v:
            tokens.add(v.lower())
            tokens.update(
                p
                for p in re.split(r"[^a-zA-Z]+", v.lower())
                if len(p) >= 4 and p not in GENERIC_CONFIG_WORDS
            )
        elif sep and k == "region_tz" and "/" in v:
            city = v.rsplit("/", 1)[-1].lower()
            if len(city) >= 4 and city not in GENERIC_CONFIG_WORDS:
                tokens.add(city)


def _configured_region_tokens(repo: Path) -> set[str]:
    tokens: set[str] = set()
    for folder in (repo / "config" / "products", repo / "config" / "profiles" / "domains"):
        if folder.is_dir():
            for yml in sorted(folder.glob("*.yaml")):
                _add_yaml_region_tokens(yml, tokens)
    return tokens


def _check_no_hardcoded_regions(repo: Path, errors: list[str]) -> None:
    region_tokens = _configured_region_tokens(repo)
    if not region_tokens:
        return
    ordered = [re.escape(t) for t in sorted(region_tokens, key=len, reverse=True)]
    word_pat = re.compile(r"\b(" + "|".join(ordered) + r")\b", re.IGNORECASE)
    exempt = {"docs/reference.md", "THIRD_PARTY_NOTICES.txt"}
    for path in sorted(repo.rglob("*")):
        if not path.is_file() or any(p in IGNORED_NAMES for p in path.parts):
            continue
        rel = path.relative_to(repo).as_posix()
        if rel.startswith(("config/", "vendor/")) or rel in exempt or path.suffix == ".lock":
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(lines, start=1):
            m = word_pat.search(line)
            if m:
                errors.append(
                    f"{rel}:{lineno}: hardcoded region/product '{m.group(0)}' outside config/. "
                    "FIX: Read dynamically from config/."
                )


def _check_doc_caps(repo: Path, pkg_root: Path, errors: list[str]) -> None:
    caps = [(repo / "AGENTS.md", 90), (repo / "docs" / "architecture.md", 200)]
    caps.extend((pkg_root / pkg / "README.md", 40) for pkg in sorted(ALLOWED_PACKAGES))
    for md_path, max_lines in caps:
        if not md_path.is_file():
            errors.append(f"{md_path.relative_to(repo)}: required doc file is missing.")
        elif len(md_path.read_text(encoding="utf-8").splitlines()) > max_lines:
            errors.append(f"{md_path.relative_to(repo)}: exceeds {max_lines} lines. FIX: Trim.")


def _check_tree_and_doc_caps(repo: Path, errors: list[str]) -> None:
    for entry in sorted(repo.iterdir()):
        is_env = entry.name.startswith(".env.") and entry.name != ".env.example"
        if entry.name in IGNORED_NAMES or entry.name.endswith(".egg-info") or is_env:
            continue
        if entry.name not in ALLOWED_TOP_LEVEL:
            errors.append(f"{entry.name}: undeclared top-level entry. FIX: Use src/ or tests/.")
    pkg_root = repo / "src" / "av_wrf"
    if pkg_root.is_dir():
        for child in sorted(pkg_root.iterdir()):
            if child.name not in (IGNORED_NAMES | ALLOWED_PACKAGES | {"__init__.py"}):
                errors.append(f"src/av_wrf/{child.name}: undeclared subpackage. FIX: Remove.")
    _check_doc_caps(repo, pkg_root, errors)


def _iter_repo_python_files(repo: Path) -> list[tuple[Path, str, str | None]]:
    found: list[tuple[Path, str, str | None]] = []
    for scan_dir in ("src", "scripts", "tests"):
        base = repo / scan_dir
        if not base.is_dir():
            continue
        for py_file in sorted(base.rglob("*.py")):
            if any(part in IGNORED_NAMES for part in py_file.parts):
                continue
            rel = py_file.relative_to(repo).as_posix()
            parts = Path(rel).parts
            is_pkg = len(parts) >= 4 and parts[:2] == ("src", "av_wrf")
            pkg = parts[2] if is_pkg and parts[2] in ALLOWED_PACKAGES else None
            found.append((py_file, rel, pkg))
    return found


def audit_repo(repo: Path) -> list[str]:
    """Run structural, region-fence, boundary, budget, paired-test, and lock checks."""
    errors: list[str] = []
    _check_tree_and_doc_caps(repo, errors)
    _check_no_hardcoded_regions(repo, errors)
    pkg_root = repo / "src" / "av_wrf"
    exports = load_public_exports(pkg_root) if pkg_root.is_dir() else {}
    for py_file, rel, pkg in _iter_repo_python_files(repo):
        if py_file.name in BANNED_FILENAMES:
            errors.append(f"{rel}: '{py_file.name}' grab-bag forbidden. FIX: Concrete name.")
        check_python_file(py_file, rel, pkg, exports, errors)
    core_dir = pkg_root / "core"
    if core_dir.is_dir():
        for mod in sorted(core_dir.glob("*.py")):
            t_path = repo / "tests" / "unit" / f"test_{mod.stem}.py"
            if mod.name != "__init__.py" and not t_path.is_file():
                errors.append(f"src/av_wrf/core/{mod.name}: missing {t_path.name}. FIX: Add test.")
    errors.extend(verify_manifest_lock(repo))
    return errors


def main() -> int:
    failures = audit_repo(ROOT)
    for item in failures:
        print(f"FAIL {item}")
    if failures:
        print(f"\ncheck_repo: {len(failures)} violation(s)")
        return 1
    print("check_repo: tree, region-fence, DAG, __all__, 100-col, budgets, and locks verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
