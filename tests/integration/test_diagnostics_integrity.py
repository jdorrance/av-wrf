"""Diagnostics hashes survive stage pruning and gate recovery and webhook claims."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody

from av_wrf.contracts import ReadyReceipt
from av_wrf.runner import (
    LoadedBundle,
    LocalFileStore,
    ObjectStore,
    attempt_stage_prefix,
    dispatch_receipt_key,
    json_bytes,
    load_bundle,
    load_ready_receipt,
    post_manifest_key,
    publish_cycle_receipt,
    reconcile_dispatch,
    sha256_hex,
)
from av_wrf.runner.s3 import S3Config, S3Store
from tests.integration import stage_sparse_config
from tests.integration.test_local_publication_contract import receipt as receipt
from tests.integration.test_publish_recovery import ROOT, _stage_attempt_with_t2


@pytest.fixture(params=("local", "s3"))
def destination(
    request: pytest.FixtureRequest, tmp_path: Path
) -> Iterator[tuple[ObjectStore, LocalFileStore]]:
    backend = LocalFileStore(tmp_path / "destination")
    if request.param == "local":
        yield backend, backend
        return
    store = S3Store("s3://bucket", S3Config("https://storage.example", "auto", "a", "b"))

    def get_object(*, Bucket: str, Key: str) -> dict[str, object]:
        raw, token = backend.get_with_token(Key)
        if raw is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": StreamingBody(BytesIO(raw), len(raw)), "ETag": token}

    def put_object(
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        IfNoneMatch: str | None = None,
        IfMatch: str | None = None,
    ) -> dict[str, str]:
        assert IfNoneMatch == "*" or IfMatch is not None
        return {"ETag": backend.compare_and_swap(Key, Body, IfMatch)}

    try:
        with (
            patch.object(store._client, "get_object", side_effect=get_object),
            patch.object(store._client, "put_object", side_effect=put_object),
        ):
            yield store, backend
    finally:
        store.close()


@dataclass(frozen=True)
class _Publication:
    config: Path
    stage: LocalFileStore
    store: ObjectStore
    backend: LocalFileStore
    bundle: LoadedBundle
    ready: ReadyReceipt

    def recover(self, on_latest: Callable[[str], None] | None = None) -> ReadyReceipt:
        return publish_cycle_receipt(
            self.config,
            self.store,
            product_id=self.bundle.product.product_id,
            cycle_partition=self.ready.cycle_partition,
            attempt_id=self.ready.attempt_id,
            bundle=self.bundle,
            stage_store=self.stage,
            now_utc_iso=self.ready.publish_origin_utc,
            on_latest=on_latest,
        )


@pytest.fixture
def publication(tmp_path: Path, destination: tuple[ObjectStore, LocalFileStore]) -> _Publication:
    store, backend = destination
    config = tmp_path / "config"
    bundle = load_bundle(config, stage_sparse_config(ROOT / "config", config))
    stage = LocalFileStore(tmp_path / "stage")
    origin = f"2026-11-12T{bundle.product.publish_origin_hour_utc:02d}:00:00Z"
    part = _stage_attempt_with_t2(config, stage, bundle, origin, "attempt-a", 285.0)
    ready = publish_cycle_receipt(
        config,
        store,
        product_id=bundle.product.product_id,
        cycle_partition=part,
        attempt_id="attempt-a",
        bundle=bundle,
        stage_store=stage,
        now_utc_iso="2020-01-01T00:00:00Z",
    )
    return _Publication(config, stage, store, backend, bundle, ready)


def _prune(publication: _Publication) -> None:
    product = publication.bundle.product.product_id
    part, attempt = publication.ready.cycle_partition, publication.ready.attempt_id
    publication.stage.delete_prefix(post_manifest_key(product, part, attempt))
    publication.stage.delete_prefix(attempt_stage_prefix(product, part, attempt))


def _published_key(ready: ReadyReceipt, name: str) -> str:
    root = f"upstream/{ready.model_id}/{ready.cycle_partition}"
    if name == "_diagnostics.json":
        root = f"{root}/{ready.zarr_relpath}".rsplit("/", 1)[0]
    return f"{root}/{name}"


def _replace_object(store: LocalFileStore, key: str, raw: bytes) -> None:
    store.compare_and_swap(key, raw, store.get_with_token(key)[1])


@pytest.mark.parametrize("pruned", (False, True))
def test_plausible_diagnostics_rewrite_blocks_recovery_and_notification(
    publication: _Publication, pruned: bool
) -> None:
    p = publication
    ready_key, diag_key = (
        _published_key(p.ready, name) for name in ("_ready.json", "_diagnostics.json")
    )
    raw = p.backend.get_bytes(diag_key)
    assert raw is not None and p.ready.diagnostics_sha256 == sha256_hex(raw)
    ready_raw = p.backend.get_bytes(ready_key)
    if pruned:
        _prune(p)
    altered = json.loads(raw)
    altered["max_cfl"] = 0.125
    assert json_bytes(altered) != raw
    _replace_object(p.backend, diag_key, json_bytes(altered))
    callback, send = Mock(), Mock(return_value=204)
    with pytest.raises(ValueError, match=r"Corrupt .*_diagnostics\.json"):
        p.recover(callback)
    with pytest.raises(ValueError, match=r"Corrupt .*_diagnostics\.json"):
        reconcile_dispatch(p.store, p.ready, send)
    callback.assert_not_called()
    send.assert_not_called()
    assert p.backend.get_bytes(f"upstream/{p.ready.model_id}/latest.json") is None
    assert p.backend.get_bytes(dispatch_receipt_key(p.ready)) is None
    assert p.backend.get_bytes(ready_key) == ready_raw


def test_pruned_intact_diagnostics_support_recovery_and_notification(
    publication: _Publication,
) -> None:
    _prune(publication)
    assert publication.recover() == publication.ready
    send = Mock(return_value=204)
    assert reconcile_dispatch(publication.store, publication.ready, send)
    send.assert_called_once_with(publication.ready)


def test_retained_manifest_cannot_rebind_committed_diagnostics(publication: _Publication) -> None:
    p = publication
    key = post_manifest_key(
        p.bundle.product.product_id, p.ready.cycle_partition, p.ready.attempt_id
    )
    raw = p.stage.get_bytes(key)
    assert raw is not None
    altered = {**json.loads(raw), "diagnostics_sha256": "0" * 64}
    _replace_object(p.stage, key, json_bytes(altered))
    callback = Mock()
    with pytest.raises(ValueError, match=r"Published (diagnostics SHA-256|forecast path) differs"):
        p.recover(callback)
    callback.assert_not_called()
    assert p.backend.get_bytes(f"upstream/{p.ready.model_id}/latest.json") is None


def test_legacy_read_and_retained_manifest_recovery_preserve_archived_receipt(
    publication: _Publication,
) -> None:
    p = publication
    legacy = replace(p.ready, diagnostics_sha256=None)
    legacy_raw = json_bytes(legacy.to_doc())
    key = _published_key(legacy, "_ready.json")
    _replace_object(p.backend, key, legacy_raw)
    assert load_ready_receipt(p.store, legacy.model_id, legacy.cycle_partition) == legacy
    assert p.recover() == legacy
    assert p.backend.get_bytes(key) == legacy_raw
    p.backend.delete_prefix(f"upstream/{legacy.model_id}/latest.json")
    _prune(p)
    callback, send = Mock(), Mock(return_value=204)
    with pytest.raises(ValueError, match="Legacy ready receipt requires"):
        p.recover(callback)
    with pytest.raises(ValueError, match="Legacy ready receipt requires"):
        reconcile_dispatch(p.store, legacy, send)
    callback.assert_not_called()
    send.assert_not_called()
    assert p.backend.get_bytes(dispatch_receipt_key(legacy)) is None
    assert p.backend.get_bytes(f"upstream/{legacy.model_id}/latest.json") is None
    assert p.backend.get_bytes(key) == legacy_raw


@pytest.mark.parametrize("digest", (None, "", "wrong", "A" * 64, "a" * 63, 1))
def test_ready_rejects_malformed_explicit_diagnostics_digest(
    receipt: ReadyReceipt, digest: object
) -> None:
    with pytest.raises(ValueError, match="diagnostics_sha256"):
        ReadyReceipt.from_doc({**receipt.to_doc(), "diagnostics_sha256": digest})
