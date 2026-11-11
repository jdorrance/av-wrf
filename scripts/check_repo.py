"""Mechanical repository structure, 5-layer DAG, 100-col, budget, and purity linter."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.sync_contract import verify_manifest_lock  # noqa: E402

ALLOWED_TOP_LEVEL = frozenset({
    ".github", ".gitignore", ".env.example", ".pre-commit-config.yaml", ".python-version",
    "AGENTS.md", "LICENSE", "README.md", "THIRD_PARTY_NOTICES.txt", "config", "docker",
    "docs", "manifest.lock", "pyproject.toml", "scripts", "src", "tach.toml",
    "tests", "uv.lock", "vendor",
})
IGNORED_NAMES = frozenset({
    ".git", "__pycache__", ".venv", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".hypothesis", ".DS_Store", ".coverage", ".env", "build", "dist", "output",
    "runs", "work", "scratch",
})
ALLOWED_PACKAGES = frozenset({"contracts", "core", "codecs", "runner", "verify", "cli"})
BANNED_FILENAMES = frozenset({"utils.py", "helpers.py", "common.py", "misc.py", "base.py"})
CONSUMER_FENCE_TOKENS = (
    "pmtiles", "lenschow", "samples.bin", "profiles.bin", "subgrid_terrain",
)
LAYER_DEPS: dict[str, frozenset[str]] = {
    "contracts": frozenset({"contracts"}),
    "core": frozenset({"contracts", "core"}),
    "codecs": frozenset({"contracts", "core", "codecs"}),
    "runner": frozenset({"contracts", "core", "codecs", "runner"}),
    "verify": frozenset({"contracts", "core", "codecs", "verify"}),
    "cli": frozenset({"contracts", "runner", "verify", "cli"}),
}
HERMETIC_BANNED_IMPORTS = frozenset({
    "os", "sys", "subprocess", "socket", "shutil", "tempfile", "pathlib",
    "urllib", "http", "httpx", "boto3", "obstore", "netCDF4", "zarr", "random", "importlib",
})
SHARED_CORE_FOUNDATIONS = frozenset({"grid", "thermo"})
MAX_FILE_LINES = 350
MAX_FUNC_LINES = 60
MAX_LINE_COLS = 100


def _check_tree_and_doc_caps(repo: Path, errors: list[str]) -> None:
    for entry in sorted(repo.iterdir()):
        if entry.name in IGNORED_NAMES or entry.name.endswith(".egg-info"):
            continue
        if entry.name.startswith(".env.") and entry.name != ".env.example":
            continue
        if entry.name not in ALLOWED_TOP_LEVEL:
            errors.append(f"{entry.name}: undeclared top-level entry. FIX: Use src/ or tests/.")
    pkg_root = repo / "src" / "av_wrf"
    if pkg_root.is_dir():
        for child in sorted(pkg_root.iterdir()):
            if child.name not in IGNORED_NAMES and child.name != "__init__.py":
                if child.name not in ALLOWED_PACKAGES:
                    errors.append(f"src/av_wrf/{child.name}: undeclared subpackage. FIX: Remove.")
    caps = [(repo / "AGENTS.md", 90), (repo / "docs" / "architecture.md", 200)]
    caps.extend((pkg_root / pkg / "README.md", 40) for pkg in sorted(ALLOWED_PACKAGES))
    for md_path, max_lines in caps:
        if not md_path.is_file():
            errors.append(f"{md_path.relative_to(repo)}: required doc file is missing.")
        elif len(md_path.read_text(encoding="utf-8").splitlines()) > max_lines:
            errors.append(f"{md_path.relative_to(repo)}: exceeds {max_lines} lines. FIX: Trim.")


def _load_public_exports(pkg_root: Path) -> dict[str, frozenset[str]]:
    exports: dict[str, frozenset[str]] = {}
    for pkg in ALLOWED_PACKAGES:
        init_py = pkg_root / pkg / "__init__.py"
        if not init_py.is_file():
            exports[pkg] = frozenset()
            continue
        tree = ast.parse(init_py.read_text(encoding="utf-8"), filename=str(init_py))
        names = {
            elt.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
            and isinstance(node.value, (ast.List, ast.Tuple))
            for elt in node.value.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        }
        exports[pkg] = frozenset(names)
    return exports


def _check_dataclass_frozen(node: ast.ClassDef, rel: str, errors: list[str]) -> None:
    for dec in node.decorator_list:
        is_dc = (isinstance(dec, ast.Name) and dec.id == "dataclass") or (
            isinstance(dec, ast.Call)
            and isinstance(dec.func, ast.Name)
            and dec.func.id == "dataclass"
        )
        if is_dc and not (
            isinstance(dec, ast.Call)
            and any(
                kw.arg == "frozen"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is True
                for kw in dec.keywords
            )
        ):
            errors.append(f"{rel}:{node.lineno}: '{node.name}' missing frozen=True. FIX: Freeze.")


def _check_ast_node(
    node: ast.AST,
    rel: str,
    pkg_name: str | None,
    is_src_or_scripts: bool,
    is_hermetic: bool,
    errors: list[str],
) -> None:
    if is_src_or_scripts and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        span = getattr(node, "end_lineno", node.lineno) - node.lineno + 1
        if span > MAX_FUNC_LINES:
            errors.append(
                f"{rel}:{node.lineno}: '{node.name}' is {span}L > {MAX_FUNC_LINES}L. FIX: Split."
            )
    if pkg_name is not None and isinstance(node, ast.ClassDef):
        _check_dataclass_frozen(node, rel, errors)
    if is_src_or_scripts and isinstance(node, ast.Name) and node.id in {"Any", "ABC"}:
        errors.append(f"{rel}:{node.lineno}: '{node.id}' forbidden. FIX: Concrete types.")
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if pkg_name is not None and node.func.id in {"eval", "exec", "__import__"}:
            errors.append(f"{rel}:{node.lineno}: '{node.func.id}()' forbidden. FIX: Static import.")
        if node.func.id == "zip" and not any(kw.arg == "strict" for kw in node.keywords):
            errors.append(
                f"{rel}:{node.lineno}: bare zip() forbidden (B905). FIX: Index or strict=True."
            )
    if pkg_name is not None and rel != "src/av_wrf/cli/env.py":
        if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}:
            errors.append(f"{rel}:{node.lineno}: os.{node.attr} forbidden. FIX: Use cli/env.py.")
    if is_hermetic and isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id == "open":
            errors.append(f"{rel}:{node.lineno}: open() forbidden in '{pkg_name}'. FIX: Move I/O.")
        if isinstance(node.func, ast.Attribute) and node.func.attr in {"now", "utcnow", "time"}:
            errors.append(f"{rel}:{node.lineno}: clock call forbidden. FIX: Pass UTC string.")
    if rel.startswith("tests/") and isinstance(node, ast.Attribute):
        if node.attr in {"skip", "skipTest", "xfail"}:
            errors.append(f"{rel}:{node.lineno}: test '{node.attr}' forbidden. FIX: Fix test.")


def _check_import_edge(
    mod: str,
    imported_names: list[str],
    lineno: int,
    rel: str,
    filename: str,
    pkg_name: str | None,
    is_hermetic: bool,
    exports: dict[str, frozenset[str]],
    errors: list[str],
) -> None:
    top = mod.split(".")[0]
    if pkg_name is not None and top == "importlib":
        errors.append(f"{rel}:{lineno}: 'importlib' forbidden. FIX: Static imports.")
    if is_hermetic and top in HERMETIC_BANNED_IMPORTS:
        errors.append(f"{rel}:{lineno}: '{mod}' forbidden in '{pkg_name}'. FIX: Move side effect.")
    if pkg_name is None or not mod.startswith("av_wrf."):
        return
    parts = mod.split(".")
    target_pkg = parts[1] if len(parts) >= 2 else ""
    if target_pkg not in ALLOWED_PACKAGES:
        return
    if target_pkg not in LAYER_DEPS[pkg_name]:
        errors.append(
            f"{rel}:{lineno}: layer violation '{pkg_name}' -> '{target_pkg}'. FIX: DAG."
        )
        return
    if pkg_name == "core" and target_pkg == "core" and filename != "__init__.py":
        if len(parts) >= 3 and parts[2] not in (SHARED_CORE_FOUNDATIONS | {filename[:-3]}):
            errors.append(f"{rel}:{lineno}: core peer import '{mod}' forbidden. FIX: Decouple.")
    if target_pkg != pkg_name:
        if len(parts) > 2 or not imported_names:
            errors.append(
                f"{rel}:{lineno}: '{mod}' bypasses '{target_pkg}.__init__'. FIX: __all__."
            )
        missing = [s for s in imported_names if s not in exports.get(target_pkg, frozenset())]
        for sym in missing:
            errors.append(
                f"{rel}:{lineno}: '{sym}' not in av_wrf.{target_pkg}.__all__. FIX: Export."
            )


def _check_python_file(
    py_file: Path,
    rel: str,
    pkg_name: str | None,
    exports: dict[str, frozenset[str]],
    errors: list[str],
) -> None:
    text = py_file.read_text(encoding="utf-8")
    lines = text.splitlines()
    if len(lines) > MAX_FILE_LINES:
        errors.append(f"{rel}: {len(lines)}L exceeds {MAX_FILE_LINES}L. FIX: Split module.")
    is_src_or_scripts = rel.startswith(("src/", "scripts/"))
    for idx, line in enumerate(lines, start=1):
        if len(line) > MAX_LINE_COLS:
            errors.append(
                f"{rel}:{idx}: {len(line)} cols exceeds {MAX_LINE_COLS} cols. FIX: Wrap."
            )
        if is_src_or_scripts:
            code_part, sep, comment_part = line.partition("#")
            if sep and code_part.count('"') % 2 == 0 and code_part.count("'") % 2 == 0:
                if comment_part.strip().replace(" ", "").startswith("type:ignore"):
                    errors.append(f"{rel}:{idx}: type-ignore is forbidden. FIX: Tighten types.")
    if pkg_name is not None:
        low = text.lower()
        for tok in CONSUMER_FENCE_TOKENS:
            if tok in low:
                errors.append(f"{rel}: consumer token '{tok}' forbidden. FIX: Keep in consumer.")
    tree = ast.parse(text, filename=rel)
    is_hermetic = pkg_name in {"contracts", "core"}
    fname = py_file.name
    for node in ast.walk(tree):
        _check_ast_node(node, rel, pkg_name, is_src_or_scripts, is_hermetic, errors)
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            errors.append(f"{rel}:{node.lineno}: relative import forbidden. FIX: Absolute import.")
        elif isinstance(node, ast.Import):
            for a in node.names:
                _check_import_edge(
                    a.name, [], node.lineno, rel, fname, pkg_name, is_hermetic, exports, errors
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [a.name for a in node.names]
            _check_import_edge(
                node.module, names, node.lineno, rel, fname, pkg_name, is_hermetic, exports, errors
            )


def audit_repo(repo: Path) -> list[str]:
    """Run all structural, boundary, budget, paired-test, and lock checks on repo."""
    errors: list[str] = []
    _check_tree_and_doc_caps(repo, errors)
    pkg_root = repo / "src" / "av_wrf"
    exports = _load_public_exports(pkg_root) if pkg_root.is_dir() else {}
    for scan_dir in ("src", "scripts", "tests"):
        base = repo / scan_dir
        if not base.is_dir():
            continue
        for py_file in sorted(base.rglob("*.py")):
            if any(part in IGNORED_NAMES for part in py_file.parts):
                continue
            rel = py_file.relative_to(repo).as_posix()
            if py_file.name in BANNED_FILENAMES:
                errors.append(f"{rel}: '{py_file.name}' grab-bag forbidden. FIX: Concrete name.")
            parts = Path(rel).parts
            pkg = (
                parts[2]
                if len(parts) >= 4
                and parts[:2] == ("src", "av_wrf")
                and parts[2] in ALLOWED_PACKAGES
                else None
            )
            _check_python_file(py_file, rel, pkg, exports, errors)
    core_dir = pkg_root / "core"
    if core_dir.is_dir():
        for mod in sorted(core_dir.glob("*.py")):
            t_path = repo / "tests" / "unit" / f"test_{mod.stem}.py"
            if mod.name != "__init__.py" and not t_path.is_file():
                errors.append(
                    f"src/av_wrf/core/{mod.name}: missing {t_path.name}. FIX: Add unit test."
                )
    errors.extend(verify_manifest_lock(repo))
    return errors


def main() -> int:
    failures = audit_repo(ROOT)
    if failures:
        for item in failures:
            print(f"FAIL {item}")
        print(f"\ncheck_repo: {len(failures)} violation(s)")
        return 1
    print("check_repo: tree, DAG, __all__, 100-col, budgets, and SHA-256 locks verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
