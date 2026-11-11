"""Fail-closed idempotent _dispatched.json webhook reconciler for windy-av-layer."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable

import httpx

from av_wrf.contracts import ReadyReceipt
from av_wrf.runner.store import ObjectStore, json_bytes, published_cycle_prefix


def dispatch_receipt_key(receipt: ReadyReceipt) -> str:
    return f"{published_cycle_prefix(receipt.model_id, receipt.cycle_partition)}/_dispatched.json"


def post_webhook_json(webhook_url: str, receipt: ReadyReceipt, timeout_s: float = 15.0) -> int:
    """POST ReadyReceipt JSON via httpx to consumer webhook endpoint; fail closed when unset."""
    clean_url = webhook_url.strip()
    if not clean_url.startswith(("http://", "https://")):
        raise ValueError(
            "Webhook URL is required for reconcile (pass --webhook-url or set AV_WRF_WEBHOOK_URL)"
        )
    resp = httpx.post(
        clean_url,
        content=json_bytes(dataclasses.asdict(receipt)),
        headers={"Content-Type": "application/json", "User-Agent": "av-wrf/0.1.0"},
        timeout=timeout_s,
    )
    return resp.status_code


def reconcile_dispatch(
    store: ObjectStore,
    receipt: ReadyReceipt,
    send_webhook: Callable[[ReadyReceipt], int],
) -> bool:
    """Idempotently dispatch a cycle receipt once; return False if already dispatched."""
    key = dispatch_receipt_key(receipt)
    if store.get_bytes(key) is not None:
        return False
    status_code = send_webhook(receipt)
    if not (200 <= status_code < 300):
        raise RuntimeError(f"Consumer webhook rejected {receipt.cycle_partition}: {status_code}")
    payload = json_bytes(
        {
            "model_id": receipt.model_id,
            "cycle_partition": receipt.cycle_partition,
            "publish_origin_utc": receipt.publish_origin_utc,
            "attempt_id": receipt.attempt_id,
            "status_code": status_code,
        }
    )
    try:
        store.put_if_absent(key, payload)
        return True
    except FileExistsError:
        return False
