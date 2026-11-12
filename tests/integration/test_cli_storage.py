"""CLI storage choices must be explicit before opening a store."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from av_wrf.cli import load_runtime_env, main
from av_wrf.runner import load_bundle

CONFIG = Path(__file__).resolve().parents[2] / "config"


class TestCliStorage(TestCase):
    def test_reconcile_rejects_missing_target_and_wrong_attempt_before_claim(self) -> None:
        product = next((CONFIG / "products").glob("*.yaml")).stem
        hour = load_bundle(CONFIG, product).product.publish_origin_hour_utc
        args = [
            "reconcile",
            "--config-dir",
            str(CONFIG),
            "--product",
            product,
            "--cycle",
            f"20261112/{hour:02d}",
            "--attempt",
            "wanted",
        ]
        with TemporaryDirectory() as tmp:
            destination = Path(tmp) / "destination"
            env = load_runtime_env({"AV_WRF_STORE_URI": destination.as_uri()})
            with (
                patch("av_wrf.cli.main.load_runtime_env", return_value=env),
                patch("av_wrf.cli.main.reconcile_dispatch") as dispatch,
                redirect_stderr(StringIO()) as stderr,
            ):
                with self.assertRaises(SystemExit):
                    main(args)
                self.assertIn("AV_WRF_WEBHOOK_URL", stderr.getvalue())
                self.assertFalse(destination.exists())
                with (
                    patch(
                        "av_wrf.cli.main.load_ready_receipt",
                        return_value=SimpleNamespace(attempt_id="committed"),
                    ),
                    self.assertRaisesRegex(ValueError, "Expected published attempt 'wanted'"),
                ):
                    main([*args, "--webhook-url", "https://example.invalid"])
                with (
                    patch(
                        "av_wrf.cli.main.load_ready_receipt",
                        return_value=SimpleNamespace(
                            attempt_id="wanted", publish_origin_utc=f"2026-11-12T{hour:02d}:30:00Z"
                        ),
                    ),
                    self.assertRaisesRegex(ValueError, "Published receipt origin"),
                ):
                    main([*args, "--webhook-url", "https://example.invalid"])
                dispatch.assert_not_called()

    def test_stage_and_destination_must_be_explicit(self) -> None:
        product = next((CONFIG / "products").glob("*.yaml")).stem
        hour = load_bundle(CONFIG, product).product.publish_origin_hour_utc
        cycle = f"20261112/{hour:02d}"
        args = ["--config-dir", str(CONFIG), "--product", product, "--cycle", cycle]
        with TemporaryDirectory() as tmp:
            stage = Path(tmp) / "stage"
            envs: tuple[tuple[str, dict[str, str], str], ...] = (
                ("plan", {}, "AV_WRF_STAGE_STORE_URI"),
                ("publish", {"AV_WRF_STAGE_STORE_URI": stage.as_uri()}, "--store-uri"),
            )
            for command, variables, diagnostic in envs:
                stderr = StringIO()
                env = load_runtime_env(variables)
                with (
                    patch("av_wrf.cli.main.load_runtime_env", return_value=env),
                    redirect_stderr(stderr),
                    self.assertRaises(SystemExit) as error,
                ):
                    main([command, *args])
                self.assertEqual(error.exception.code, 2)
                self.assertIn(diagnostic, stderr.getvalue())
            self.assertFalse(stage.exists())

            env = load_runtime_env(
                {"AV_WRF_STAGE_STORE_URI": stage.as_uri(), "AV_WRF_BUILD_MANIFEST": str(stage)}
            )
            with (
                patch("av_wrf.cli.main.load_runtime_env", return_value=env),
                redirect_stdout(StringIO()),
            ):
                plan_args = ["plan", *args, "--allow-unpinned-dev"]
                self.assertEqual(main(plan_args), 0)
                self.assertEqual(main(plan_args), 0)
            self.assertEqual(len(list(stage.rglob("run.lock.json"))), 1)
