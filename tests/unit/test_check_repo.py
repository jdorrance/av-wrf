"""Audit real ROOT repository plus negative agent-chaos and region-hardcoding mutations."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.check_repo import audit_repo  # noqa: E402
from scripts.generate_reference import check_markdown_files  # noqa: E402


class TestCheckRepoHarness(unittest.TestCase):
    def test_real_shipped_root_passes_with_zero_violations(self) -> None:
        self.assertEqual(audit_repo(ROOT), [])

    def test_tolerates_local_artifacts_and_catches_agent_loopholes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            clone = Path(tmp) / "repo"
            shutil.copytree(
                ROOT,
                clone,
                ignore=shutil.ignore_patterns(
                    ".git", "__pycache__", "*.pyc", ".venv", ".import_linter_cache"
                ),
            )
            (clone / "uv.lock").write_text("version = 1\n", encoding="utf-8")
            (clone / ".env").write_text("AV_WRF_MPI_RANKS=8\n", encoding="utf-8")
            (clone / "output" / "test").mkdir(parents=True)
            (clone / "runs" / "test").mkdir(parents=True)
            local_plan = clone / ".kilo" / "plans" / "local-review.md"
            local_plan.parent.mkdir(parents=True, exist_ok=True)
            local_plan.write_text("Local editor notes.\n", encoding="utf-8")
            self.assertEqual(audit_repo(clone), [])
            self.assertEqual(check_markdown_files(clone), [])

            # 1. Dumping-ground utils.py is rejected
            bad_utils = clone / "src" / "av_wrf" / "core" / "utils.py"
            bad_utils.write_text("x = 1\n", encoding="utf-8")
            self.assertTrue(any("utils.py" in e and "FIX:" in e for e in audit_repo(clone)))
            bad_utils.unlink()

            # 2. Hardcoding a configured product ID in Dockerfile is rejected
            first_prod = sorted((clone / "config" / "products").glob("*.yaml"))[0].stem
            dockerfile = clone / "docker" / "Dockerfile"
            orig_df = dockerfile.read_text(encoding="utf-8")
            dockerfile.write_text(orig_df + f'\n# CMD ["check", "--product", "{first_prod}"]\n')
            self.assertTrue(any("hardcoded region/product" in e for e in audit_repo(clone)))
            dockerfile.write_text(orig_df, encoding="utf-8")

            # 3. Upward DAG, relative import, E501, B905 zip, mutable dataclass, & type-ignore
            grid_py = clone / "src" / "av_wrf" / "core" / "grid.py"
            orig_grid = grid_py.read_text(encoding="utf-8")
            ti_comment = "# type" + ": ignore"
            long_line = "X = '" + ("a" * 105) + "'"
            grid_py.write_text(
                orig_grid
                + "\nimport os\nfrom dataclasses import dataclass\n"
                + "from ..runner import open_store\n"
                + f"from av_wrf.runner import LocalFileStore  {ti_comment}\n"
                + f"{long_line}\n_ = list(zip([1], [2]))\n"
                + "@dataclass\nclass BadMutable:\n    x: int\n"
                + "import abc, builtins, dataclasses, typing\n"
                + "@dataclasses.dataclass\nclass QualifiedMutable:\n    value: typing.Any\n"
                + "class Abstract(abc.ABC):\n    pass\n"
                + "_ = builtins.open('data')\n"
                + "# pmtiles\n_ = os.environ\n_ = eval('1')\n",
                encoding="utf-8",
            )
            errs2 = audit_repo(clone)
            for token in (
                "layer violation",
                "relative import",
                "exceeds 100 cols",
                "B905",
                "frozen=True",
                "QualifiedMutable",
                "'Any' forbidden",
                "'ABC' forbidden",
                "open() forbidden",
                "type-ignore",
                "eval",
                "environ",
                "pmtiles",
            ):
                self.assertTrue(any(token in e for e in errs2), msg=f"Missing check for {token}")
            grid_py.write_text(orig_grid, encoding="utf-8")

            # 4. Unauthorized edit to vendor/producer-contract fails SHA-256 lock
            schema = clone / "vendor" / "producer-contract" / "wrf-zarr-v3.schema.json"
            schema.write_text(schema.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            self.assertTrue(any("SHA-256 mismatch" in e for e in audit_repo(clone)))


if __name__ == "__main__":
    unittest.main()
