"""Single-flight destination-scoped webhook reconciler with deterministic idempotency keys."""

from __future__ import annotations

import contextlib
import dataclasses
import json
from collections.abc import Callable

import httpx

from av_wrf.contracts import ReadyReceipt
from av_wrf.runner.store import ObjectStore, json_bytes, published_cycle_prefix, sha256_hex


def dispatch_idempotency_key(receipt: ReadyReceipt, destination_id: str = "default") -> str:
    """Return stable deduplication key bound to model, cycle, attempt, and destination."""
    dest_tag = sha256_hex(destination_id.strip().encode("utf-8"))[:16]
    return f"{receipt.model_id}:{receipt.cycle_partition}:{receipt.attempt_id}:{dest_tag}"


def dispatch_receipt_key(receipt: ReadyReceipt, destination_id: str = "default") -> str:
    """Return per-destination dispatch state key under the published cycle prefix."""
    dest_tag = sha256_hex(destination_id.strip().encode("utf-8"))[:16]
    return (
        f"{published_cycle_prefix(receipt.model_id, receipt.cycle_partition)}"
        f"/_dispatched/{dest_tag}.json"
    )


def post_webhook_json(webhook_url: str, receipt: ReadyReceipt, timeout_s: float = 15.0) -> int:
    """POST ReadyReceipt JSON with stable Idempotency-Key header; fail closed when unset."""
    clean_url = webhook_url.strip()
    if not clean_url.startswith(("http://", "https://")):
        raise ValueError(
            "Webhook URL is required for reconcile (pass --webhook-url or set AV_WRF_WEBHOOK_URL)"
        )
    idem_key = dispatch_idempotency_key(receipt, clean_url)
    resp = httpx.post(
        clean_url,
        content=json_bytes(dataclasses.asdict(receipt)),
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": idem_key,
            "User-Agent": "av-wrf/0.1.0",
        },
        timeout=timeout_s,
    )
    return resp.status_code


def _claim_dispatch_slot(
    store: ObjectStore, key: str, receipt: ReadyReceipt, idem_key: str
) -> str | None:
    """Atomically claim `None`/`failed` -> `in_flight`; return claim SHA-256 or None if owned."""
    claim_bytes = json_bytes(
        {
            "state": "in_flight",
            "idempotency_key": idem_key,
            "model_id": receipt.model_id,
            "cycle_partition": receipt.cycle_partition,
            "attempt_id": receipt.attempt_id,
        }
    )
    for _ in range(4):
        raw = store.get_bytes(key)
        expected_sha: str | None = None
        if raw is not None:
            doc = json.loads(raw.decode("utf-8"))
            if isinstance(doc, dict) and doc.get("state") in ("delivered", "in_flight"):
                return None
            expected_sha = sha256_hex(raw)
        try:
            return store.compare_and_swap(key, claim_bytes, expected_sha)
        except RuntimeError:
            continue
    return None


def reconcile_dispatch(
    store: ObjectStore,
    receipt: ReadyReceipt,
    send_webhook: Callable[[ReadyReceipt], int],
    *,
    destination_id: str = "default",
) -> bool:
    """Single-flight per-destination webhook delivery; returns False if already sent/claimed."""
    key = dispatch_receipt_key(receipt, destination_id)
    idem_key = dispatch_idempotency_key(receipt, destination_id)
    claim_sha = _claim_dispatch_slot(store, key, receipt, idem_key)
    if claim_sha is None:
        return False
    try:
        status_code = send_webhook(receipt)
        if not (200 <= status_code < 300):
            raise RuntimeError(
                f"Consumer webhook rejected {receipt.cycle_partition}: {status_code}"
            )
    except Exception:
        failed_doc = json_bytes(
            {
                "state": "failed",
                "idempotency_key": idem_key,
                "model_id": receipt.model_id,
                "cycle_partition": receipt.cycle_partition,
                "attempt_id": receipt.attempt_id,
            }
        )
        with contextlib.suppress(RuntimeError):
            store.compare_and_swap(key, failed_doc, claim_sha)
        raise

    delivered_doc = json_bytes(
        {
            "state": "delivered",
            "idempotency_key": idem_key,
            "model_id": receipt.model_id,
            "cycle_partition": receipt.cycle_partition,
            "publish_origin_utc": receipt.publish_origin_utc,
            "attempt_id": receipt.attempt_id,
            "status_code": status_code,
        }
    )
    store.compare_and_swap(key, delivered_doc, claim_sha)
    return True
