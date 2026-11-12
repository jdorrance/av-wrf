"""AST, `__all__` export, line-hygiene, and purity checks reading DAG from pyproject.toml."""

from __future__ import annotations

import ast
import functools
import tomllib
from pathlib import Path

CONSUMER_FENCE_TOKENS = ("pmtiles", "lenschow", "samples.bin", "profiles.bin", "subgrid_terrain")
# `namelist` and `nesting` are pure primitive modules shared by the WPS compiler and core
# namelist validation, so they are foundations alongside grid/thermo/vertical.
SHARED_CORE_FOUNDATIONS = frozenset(
    {"grid", "thermo", "vertical", "namelist", "nesting", "physics_namelist", "post_3d"}
)
MAX_FILE_LINES, MAX_FUNC_LINES, MAX_LINE_COLS = 350, 60, 100
_ROOT_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


@functools.cache
def _load_dag_spec(
    pyproject_path: Path = _ROOT_PYPROJECT,
) -> tuple[frozenset[str], dict[str, frozenset[str]], frozenset[str]]:
    doc = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    contracts = doc["tool"]["importlinter"]["contracts"]
    layer_rows: list[list[str]] = []
    banned: set[str] = set()
    for entry in contracts:
        if entry.get("type") == "layers":
            layer_rows = [[p.strip() for p in row.split("|")] for row in entry["layers"]]
        elif entry.get("type") == "forbidden":
            banned.update(entry["forbidden_modules"])
    all_pkgs = frozenset(pkg for row in layer_rows for pkg in row)
    deps: dict[str, frozenset[str]] = {}
    for idx, row in enumerate(layer_rows):
        lower = {p for lower_row in layer_rows[idx + 1 :] for p in lower_row}
        if row == ["cli"]:
            lower -= {"core", "codecs"}
        for pkg in row:
            deps[pkg] = frozenset({pkg} | lower)
    return all_pkgs, deps, frozenset(banned)


ALLOWED_PACKAGES: frozenset[str] = _load_dag_spec()[0]


def load_public_exports(pkg_root: Path) -> dict[str, frozenset[str]]:
    exports: dict[str, frozenset[str]] = {}
    for pkg in _load_dag_spec()[0]:
        init_py = pkg_root / pkg / "__init__.py"
        if not init_py.is_file():
            exports[pkg] = frozenset()
            continue
        tree = ast.parse(init_py.read_text(encoding="utf-8"), filename=str(init_py))
        exports[pkg] = frozenset(
            elt.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
            and isinstance(node.value, (ast.List, ast.Tuple))
            for elt in node.value.elts
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
        )
    return exports


def _check_dataclass_frozen(node: ast.ClassDef, rel: str, errors: list[str]) -> None:
    for dec in node.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        is_dc = (isinstance(target, ast.Name) and target.id == "dataclass") or (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and (target.value.id, target.attr) == ("dataclasses", "dataclass")
        )
        frozen_ok = isinstance(dec, ast.Call) and any(
            kw.arg == "frozen" and isinstance(kw.value, ast.Constant) and kw.value.value is True
            for kw in dec.keywords
        )
        if is_dc and not frozen_ok:
            errors.append(f"{rel}:{node.lineno}: '{node.name}' missing frozen=True. FIX: Freeze.")


def _check_call_node(
    node: ast.Call, rel: str, pkg_name: str | None, is_hermetic: bool, errors: list[str]
) -> None:
    if isinstance(node.func, ast.Name):
        if pkg_name is not None and node.func.id in {"eval", "exec", "__import__"}:
            errors.append(f"{rel}:{node.lineno}: '{node.func.id}()' forbidden. FIX: Static import.")
        if node.func.id == "zip" and not any(kw.arg == "strict" for kw in node.keywords):
            errors.append(
                f"{rel}:{node.lineno}: bare zip() forbidden (B905). FIX: Index or strict=True."
            )
        if is_hermetic and node.func.id == "open":
            errors.append(f"{rel}:{node.lineno}: open() forbidden in '{pkg_name}'. FIX: Move I/O.")
    elif is_hermetic and isinstance(node.func, ast.Attribute):
        if node.func.attr in {"now", "utcnow", "time"}:
            errors.append(f"{rel}:{node.lineno}: clock call forbidden. FIX: Pass UTC string.")
        if (
            node.func.attr == "open"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "builtins"
        ):
            errors.append(f"{rel}:{node.lineno}: open() forbidden in '{pkg_name}'. FIX: Move I/O.")


def _check_ast_node(
    node: ast.AST, rel: str, pkg: str | None, is_code: bool, is_hermetic: bool, errors: list[str]
) -> None:
    if is_code and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        span = getattr(node, "end_lineno", node.lineno) - node.lineno + 1
        if span > MAX_FUNC_LINES:
            errors.append(
                f"{rel}:{node.lineno}: '{node.name}' is {span}L > {MAX_FUNC_LINES}L. FIX: Split."
            )
    if pkg is not None and isinstance(node, ast.ClassDef):
        _check_dataclass_frozen(node, rel, errors)
    if is_code and isinstance(node, ast.Name) and node.id in {"Any", "ABC"}:
        errors.append(f"{rel}:{node.lineno}: '{node.id}' forbidden. FIX: Concrete types.")
    if (
        is_code
        and isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and (node.value.id, node.attr) in {("typing", "Any"), ("abc", "ABC")}
    ):
        errors.append(f"{rel}:{node.lineno}: '{node.attr}' forbidden. FIX: Concrete types.")
    if isinstance(node, ast.Call):
        _check_call_node(node, rel, pkg, is_hermetic, errors)
    if (
        pkg is not None
        and rel != "src/av_wrf/cli/env.py"
        and isinstance(node, ast.Attribute)
        and node.attr in {"environ", "getenv"}
    ):
        errors.append(f"{rel}:{node.lineno}: os.{node.attr} forbidden. FIX: Use cli/env.py.")
    if (
        rel.startswith("tests/")
        and isinstance(node, ast.Attribute)
        and node.attr in {"skip", "skipTest", "xfail"}
    ):
        errors.append(f"{rel}:{node.lineno}: test '{node.attr}' forbidden. FIX: Fix test.")


def _check_internal_import(
    parts: list[str],
    names: list[str],
    ln: int,
    rel: str,
    fname: str,
    pkg: str,
    exports: dict[str, frozenset[str]],
    errors: list[str],
) -> None:
    allowed_pkgs, layer_deps, _ = _load_dag_spec()
    target = parts[1] if len(parts) >= 2 else ""
    if target not in allowed_pkgs:
        return
    if target not in layer_deps[pkg]:
        errors.append(f"{rel}:{ln}: layer violation '{pkg}' -> '{target}'. FIX: DAG.")
        return
    if (
        pkg == "core"
        and target == "core"
        and fname != "__init__.py"
        and len(parts) >= 3
        and parts[2] not in (SHARED_CORE_FOUNDATIONS | {fname[:-3]})
    ):
        errors.append(f"{rel}:{ln}: core peer import '{'.'.join(parts)}' forbidden. FIX: Decouple.")
    if target != pkg:
        if len(parts) > 2 or not names:
            errors.append(
                f"{rel}:{ln}: '{'.'.join(parts)}' bypasses '{target}.__init__'. FIX: __all__."
            )
        for sym in (s for s in names if s not in exports.get(target, frozenset())):
            errors.append(f"{rel}:{ln}: '{sym}' not in av_wrf.{target}.__all__. FIX: Export.")


def _check_import_edge(
    mod: str,
    names: list[str],
    ln: int,
    rel: str,
    fname: str,
    pkg: str | None,
    is_hermetic: bool,
    exports: dict[str, frozenset[str]],
    errors: list[str],
) -> None:
    _, _, banned_imports = _load_dag_spec()
    top = mod.split(".")[0]
    if pkg is not None and top == "importlib":
        errors.append(f"{rel}:{ln}: 'importlib' forbidden. FIX: Static imports.")
    if is_hermetic and top in banned_imports:
        errors.append(f"{rel}:{ln}: '{mod}' forbidden in '{pkg}'. FIX: Move side effect.")
    if pkg is not None and mod.startswith("av_wrf."):
        _check_internal_import(mod.split("."), names, ln, rel, fname, pkg, exports, errors)


def _check_line_hygiene(lines: list[str], rel: str, is_code: bool, errors: list[str]) -> None:
    if len(lines) > MAX_FILE_LINES:
        errors.append(f"{rel}: {len(lines)}L exceeds {MAX_FILE_LINES}L. FIX: Split module.")
    for idx, line in enumerate(lines, start=1):
        if len(line) > MAX_LINE_COLS:
            errors.append(f"{rel}:{idx}: {len(line)} cols exceeds {MAX_LINE_COLS} cols. FIX: Wrap.")
        code_part, sep, comment = line.partition("#")
        if (
            is_code
            and sep
            and code_part.count('"') % 2 == 0
            and code_part.count("'") % 2 == 0
            and comment.strip().replace(" ", "").startswith("type:ignore")
        ):
            errors.append(f"{rel}:{idx}: type-ignore is forbidden. FIX: Tighten types.")


def check_python_file(
    py_file: Path,
    rel: str,
    pkg: str | None,
    exports: dict[str, frozenset[str]],
    errors: list[str],
) -> None:
    text = py_file.read_text(encoding="utf-8")
    is_code = rel.startswith(("src/", "scripts/"))
    _check_line_hygiene(text.splitlines(), rel, is_code, errors)
    if pkg is not None:
        low = text.lower()
        errors.extend(
            f"{rel}: consumer token '{tok}' forbidden. FIX: Keep in consumer."
            for tok in CONSUMER_FENCE_TOKENS
            if tok in low
        )
    tree = ast.parse(text, filename=rel)
    is_hermetic, fname = pkg in {"contracts", "core"}, py_file.name
    for node in ast.walk(tree):
        _check_ast_node(node, rel, pkg, is_code, is_hermetic, errors)
        if isinstance(node, ast.ImportFrom) and node.level > 0:
            errors.append(f"{rel}:{node.lineno}: relative import forbidden. FIX: Absolute import.")
        elif isinstance(node, ast.Import):
            for a in node.names:
                _check_import_edge(
                    a.name, [], node.lineno, rel, fname, pkg, is_hermetic, exports, errors
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            _check_import_edge(
                node.module,
                [a.name for a in node.names],
                node.lineno,
                rel,
                fname,
                pkg,
                is_hermetic,
                exports,
                errors,
            )
