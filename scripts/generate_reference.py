"""Single-owner generator and Markdown link checker for docs/reference.md."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for extra in (ROOT, ROOT / "src"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from av_wrf.cli import CLI_COMMANDS  # noqa: E402
from av_wrf.contracts import (  # noqa: E402
    DEFAULT_INNER_SPATIAL_CHUNK,
    DEFAULT_ZSTD_LEVEL,
    MAX_EXPORTED_LEVELS,
    MOISTURE_SOURCE_OF_TRUTH,
    WRF_SPHERE_RADIUS_M,
    WRF_ZARR_V3_FIELDS,
    ZARR_SCHEMA_ID,
    ZARR_SCHEMA_STATUS,
    ZARR_SCHEMA_VERSION,
)
from av_wrf.runner import check_product_config  # noqa: E402
from scripts.check_repo import IGNORED_NAMES  # noqa: E402

LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)]+)\)")
FENCE_RE = re.compile(r"```[\s\S]*?```")
DECLARED_MARKDOWN: frozenset[str] = frozenset(
    {
        "AGENTS.md",
        "README.md",
        "docs/architecture.md",
        "docs/plan.md",
        "docs/review-plan.md",
        "docs/reference.md",
        "docs/science.md",
        "config/README.md",
        "docker/README.md",
        "docker/patches/README.md",
        "vendor/producer-contract/README.md",
        "src/av_wrf/contracts/README.md",
        "src/av_wrf/core/README.md",
        "src/av_wrf/codecs/README.md",
        "src/av_wrf/runner/README.md",
        "src/av_wrf/verify/README.md",
        "src/av_wrf/cli/README.md",
    }
)


def _render_products_section(repo: Path) -> list[str]:
    out = [
        "",
        "## Configured Products",
        "",
        "Source: [`config/products/`](../config/README.md).",
        "",
    ]
    for yaml_path in sorted((repo / "config" / "products").glob("*.yaml")):
        info = check_product_config(repo / "config", yaml_path.stem)
        nj, ni = info.published_yx
        recipe = "recipe available" if info.candidate_recipe_available else "no solve recipe"
        out.append(
            f"- `{info.product_id}`: published grid `{ni} x {nj}` (`x0M = {info.x0_m:.1f}`, "
            f"`y0M = {info.y0_m:.1f}`), primary `{info.primary_source}` "
            f"(candidate `{info.candidate_source}`, {recipe}), `{info.lead_count}` leads, "
            f"`{info.bl_levels_2km}` BL levels (`0-2 km` AGL), "
            f"remaining-horizon floor `{info.remaining_horizon_floor_hours} h`, "
            f"`time_step = {info.time_step_s} s`."
        )
    return out


def render_reference_markdown(repo: Path) -> str:
    """Render docs/reference.md directly from code contracts and config profiles."""
    lines: list[str] = [
        "# Generated Reference",
        "",
        "Generated from code schemas and `config/`. Regenerate after schema or profile edits:",
        "",
        "```sh",
        "python3 scripts/generate_reference.py",
        "```",
        "",
        "## CLI Subcommands",
        "",
        "Source: [`src/av_wrf/cli/main.py`](../src/av_wrf/cli/main.py).",
        "",
        *(f"- `av-wrf {cmd}`: {desc}" for cmd, desc in CLI_COMMANDS),
        *_render_products_section(repo),
    ]
    src_link = "[`zarr_schema.py`](../src/av_wrf/contracts/zarr_schema.py)"
    ven_link = "[`wrf-zarr-v3.schema.json`](../vendor/producer-contract/wrf-zarr-v3.schema.json)"
    lines.extend(
        [
            "",
            f"## Wire Schema `{ZARR_SCHEMA_ID}` (v{ZARR_SCHEMA_VERSION}, `{ZARR_SCHEMA_STATUS}`)",
            "",
            f"Source: {src_link} and {ven_link}.",
            "",
            f"- Spherical radius: `{WRF_SPHERE_RADIUS_M}` m; exported levels ceiling: "
            f"`{MAX_EXPORTED_LEVELS}`; moisture source of truth: `{MOISTURE_SOURCE_OF_TRUTH}`; "
            f"default inner chunk: `{list(DEFAULT_INNER_SPATIAL_CHUNK)}`; zstd level: "
            f"`{DEFAULT_ZSTD_LEVEL}`.",
            "",
            "| Field | Dimensions | Units | `ModelLevelFrame` Target | Time Support | `keepbits` |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
    )
    for f in WRF_ZARR_V3_FIELDS:
        dims = ", ".join(f.dims)
        lines.append(
            f"| `{f.name}` | `({dims})` | `{f.units}` | `{f.target}` | "
            f"`{f.time_support}` | `{f.keepbits}` |"
        )
    lines.append("")
    return "\n".join(lines)


def check_markdown_files(repo: Path) -> list[str]:
    """Verify generated reference freshness, ban orphan .md files, and validate links."""
    errors: list[str] = []
    expected_ref = render_reference_markdown(repo)
    ref_path = repo / "docs" / "reference.md"
    actual_ref = ref_path.read_text(encoding="utf-8") if ref_path.is_file() else ""
    if actual_ref != expected_ref:
        errors.append(
            "docs/reference.md is stale. FIX: Run `python3 scripts/generate_reference.py`."
        )

    found_md: set[str] = set()
    for md in sorted(repo.rglob("*.md")):
        if any(p in IGNORED_NAMES for p in md.parts):
            continue
        rel = md.relative_to(repo).as_posix()
        found_md.add(rel)
        if rel not in DECLARED_MARKDOWN:
            errors.append(f"{rel}: orphan Markdown file. FIX: Declare or merge into docs/.")
        stripped = FENCE_RE.sub("", md.read_text(encoding="utf-8"))
        for match in LINK_RE.finditer(stripped):
            raw_target = match.group(1).strip().split()[0].strip("<>")
            if re.match(r"^(?:[a-z]+:|/|#)", raw_target, re.IGNORECASE):
                continue
            clean_rel = raw_target.split("#", 1)[0]
            if clean_rel and not (md.parent / clean_rel).exists():
                errors.append(f"{rel}: broken link '{raw_target}'. FIX: Point to existing file.")

    for req in sorted(DECLARED_MARKDOWN - found_md):
        errors.append(f"{req}: declared Markdown file is missing.")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate or check docs/reference.md.")
    parser.add_argument("--check", action="store_true", help="Verify reference sync and links")
    args = parser.parse_args()
    if args.check:
        failures = check_markdown_files(ROOT)
        if failures:
            for f in failures:
                print(f"FAIL {f}")
            return 1
        print(f"generate_reference: {len(DECLARED_MARKDOWN)} Markdown files verified")
        return 0
    out_path = ROOT / "docs" / "reference.md"
    out_path.write_text(render_reference_markdown(ROOT), encoding="utf-8")
    print("Generated docs/reference.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
