"""Product checks and planning fence publication namespaces before creating a run lock."""

from __future__ import annotations

import shutil
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import yaml

from av_wrf.contracts import BuildManifest
from av_wrf.runner import (
    LoadedBundle,
    LocalFileStore,
    check_all_products,
    check_product_config,
    load_bundle,
    parse_declarative_yaml,
    plan_product_cycle,
)

CONFIG = Path(__file__).resolve().parents[2] / "config"


class TestConfigAdmission(TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.config = shutil.copytree(CONFIG, self.root / "config")
        self.product_path = next((self.config / "products").glob("*.yaml"))
        self.bundle = load_bundle(self.config, self.product_path.stem)
        self.store = LocalFileStore(self.root / "store")

    def _duplicate_product(self) -> None:
        product = parse_declarative_yaml(self.product_path)
        product["product_id"] = "duplicate-product"
        (self.config / "products" / "duplicate-product.yaml").write_text(yaml.safe_dump(product))

    def _plan(self, bundle: LoadedBundle | None) -> None:
        plan_product_cycle(
            self.config,
            self.store,
            product_id=self.product_path.stem,
            publish_origin_utc=(
                f"2026-11-12T{self.bundle.product.publish_origin_hour_utc:02d}:00:00Z"
            ),
            attempt_id="namespace-check",
            mpi_ranks=1,
            build_manifest=BuildManifest("host-dev-unpinned", "wrf", "wps", (), "gcc", "mpi"),
            bundle=bundle,
        )

    def test_both_config_check_entrypoints_reject_duplicate_models(self) -> None:
        self._duplicate_product()
        with self.assertRaisesRegex(ValueError, "distinct model_id publication namespaces"):
            check_all_products(self.config)
        with self.assertRaisesRegex(ValueError, "distinct model_id publication namespaces"):
            check_product_config(self.config, self.product_path.stem)

    def test_planning_rejects_duplicate_models_with_or_without_preloaded_bundle(self) -> None:
        self._duplicate_product()
        for bundle in (None, self.bundle):
            with (
                self.subTest(preloaded=bundle is not None),
                self.assertRaisesRegex(ValueError, "distinct model_id publication namespaces"),
            ):
                self._plan(bundle)
        self.assertEqual(list((self.root / "store").rglob("run.lock.json")), [])

    def test_planning_rejects_bundle_identity_that_disagrees_with_configuration(self) -> None:
        for product in (
            replace(self.bundle.product, product_id="another-product"),
            replace(self.bundle.product, model_id="another-model"),
        ):
            with (
                self.subTest(product=product.product_id, model=product.model_id),
                self.assertRaisesRegex(ValueError, "Bundle product identity differs"),
            ):
                self._plan(replace(self.bundle, product=product))
        self.assertEqual(list((self.root / "store").rglob("run.lock.json")), [])

    def test_namespace_scan_requires_product_identity_to_match_filename(self) -> None:
        product = parse_declarative_yaml(self.product_path)
        product["product_id"] = "wrong-filename"
        self.product_path.write_text(yaml.safe_dump(product))
        with self.assertRaisesRegex(ValueError, "product_id must match the filename"):
            self._plan(self.bundle)
