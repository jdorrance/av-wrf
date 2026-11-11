"""Idempotent _dispatched.json webhook reconciler for windy-av-layer."""

from __future__ import annotations

import json
from collections.abc import Callable

from av_wrf.contracts import ReadyReceipt
from av_wrf.runner.store import ObjectStore


def dispatch_receipt_key(receipt: ReadyReceipt) -> str:
    return f"upstream/{receipt.model_id}/{receipt.cycle_partition}/_dispatched.json"


def reconcile_dispatch(
    store: ObjectStore,
    receipt: ReadyReceipt,
    send_webhook: Callable[[ReadyReceipt], int] | None = None,
) -> bool:
    """Idempotently dispatch a cycle receipt once; return False if already dispatched."""
    key = dispatch_receipt_key(receipt)
    if store.get_bytes(key) is not None:
        return False
    status_code = 202 if send_webhook is None else send_webhook(receipt)
    if not (200 <= status_code < 300):
        raise RuntimeError(f"Consumer webhook rejected {receipt.cycle_partition}: {status_code}")
    payload = json.dumps(
        {
            "model_id": receipt.model_id,
            "cycle_partition": receipt.cycle_partition,
            "publish_origin_utc": receipt.publish_origin_utc,
            "attempt_id": receipt.attempt_id,
            "status_code": status_code,
        },
        sort_keys=True,
    ).encode("utf-8")
    try:
        store.put_if_absent(key, payload)
        return True
    except FileExistsError:
        return False
