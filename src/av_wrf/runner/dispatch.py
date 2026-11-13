"""Single-flight destination-scoped webhook reconciler with expiring leases and CAS takeover."""

from __future__ import annotations

import contextlib
import json
import math
import re
import time
import uuid
from collections.abc import Callable
from time import monotonic

import httpx

from av_wrf.contracts import ReadyReceipt
from av_wrf.runner.publish_verify import verify_published_diagnostics
from av_wrf.runner.store import (
    CASConflict,
    ObjectStore,
    json_bytes,
    published_cycle_prefix,
    sha256_hex,
)

DEFAULT_DISPATCH_LEASE_S = 300.0


def _destination_tag(destination_id: str) -> str:
    return sha256_hex(destination_id.strip().encode("utf-8"))[:16]


def dispatch_idempotency_key(receipt: ReadyReceipt, destination_id: str = "default") -> str:
    """Return stable deduplication key bound to model, cycle, attempt, and destination."""
    dest_tag = _destination_tag(destination_id)
    return f"{receipt.model_id}:{receipt.cycle_partition}:{receipt.attempt_id}:{dest_tag}"


def dispatch_receipt_key(receipt: ReadyReceipt, destination_id: str = "default") -> str:
    """Return per-destination dispatch state key under the published cycle prefix."""
    dest_tag = _destination_tag(destination_id)
    return (
        f"{published_cycle_prefix(receipt.model_id, receipt.cycle_partition)}"
        f"/_dispatched/{dest_tag}.json"
    )


def post_webhook_json(
    webhook_url: str, receipt: ReadyReceipt, timeout_s: float = 15.0, *, bearer_token: str = ""
) -> int:
    """POST ReadyReceipt over TLS with a stable idempotency key and optional bearer auth."""
    clean_url = webhook_url.strip()
    try:
        url = httpx.URL(clean_url)
    except httpx.InvalidURL:
        raise ValueError("Invalid HTTPS webhook URL") from None
    if url.scheme != "https" or not url.host or url.userinfo or url.query or url.fragment:
        raise ValueError(
            "Webhook URL requires HTTPS with no embedded credentials, query or fragment"
        )
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": dispatch_idempotency_key(receipt, clean_url),
        "User-Agent": "av-wrf/0.1.0",
    }
    if bearer_token:
        if not re.fullmatch(r"[A-Za-z0-9._~+/-]+=*", bearer_token):
            raise ValueError("Invalid webhook bearer token")
        headers["Authorization"] = f"Bearer {bearer_token}"
    try:
        resp = httpx.post(
            url,
            content=json_bytes(receipt.to_doc()),
            headers=headers,
            timeout=timeout_s,
            follow_redirects=False,
        )
    except (httpx.RequestError, httpx.InvalidURL):
        raise RuntimeError("Consumer webhook request failed") from None
    return resp.status_code


def _is_active_inflight_lease(doc: dict[str, object], now_s: float) -> bool:
    if doc.get("state") != "in_flight":
        return False
    claimed_at = doc.get("claimed_at_unix_s")
    lease_s = doc.get("lease_seconds", DEFAULT_DISPATCH_LEASE_S)
    if claimed_at is None:
        return False
    if not (
        isinstance(claimed_at, (int, float))
        and math.isfinite(claimed_at)
        and isinstance(lease_s, (int, float))
        and math.isfinite(lease_s)
        and lease_s > 0
    ):
        raise ValueError("Invalid stored dispatch lease")
    return (now_s - float(claimed_at)) < float(lease_s)


def _claim_dispatch_slot(
    store: ObjectStore,
    key: str,
    identity: dict[str, str],
    now_s: float,
    lease_seconds: float,
) -> str | None:
    """Atomically claim `None`/`failed`/expired `in_flight` -> `in_flight` with lease token."""
    claim_bytes = json_bytes(
        {
            "state": "in_flight",
            "claim_id": uuid.uuid4().hex,
            "claimed_at_unix_s": round(now_s, 3),
            "lease_seconds": lease_seconds,
            **identity,
        }
    )
    raw, expected_tok = store.get_with_token(key)
    for _ in range(4):
        if raw is not None:
            doc = json.loads(raw.decode("utf-8"))
            if not isinstance(doc, dict) or doc.get("state") not in (
                "delivered",
                "failed",
                "in_flight",
            ):
                raise ValueError("Invalid stored dispatch state")
            if any(doc.get(field, value) != value for field, value in identity.items()):
                raise ValueError("Stored dispatch state belongs to a different delivery")
            if doc["state"] == "delivered" or _is_active_inflight_lease(doc, now_s):
                return None
        try:
            return store.compare_and_swap(key, claim_bytes, expected_tok)
        except CASConflict:
            # A conditional retry can conflict with our own already committed claim.
            raw, expected_tok = store.get_with_token(key)
            if raw == claim_bytes:
                return expected_tok
    raise CASConflict("Dispatch state changed repeatedly while claiming delivery")


def reconcile_dispatch(
    store: ObjectStore,
    receipt: ReadyReceipt,
    send_webhook: Callable[[ReadyReceipt], int],
    *,
    destination_id: str = "default",
    lease_seconds: float = DEFAULT_DISPATCH_LEASE_S,
    now_unix_s: float | None = None,
) -> bool:
    """Single-flight per-destination webhook delivery with expiring lease recovery."""
    root = published_cycle_prefix(receipt.model_id, receipt.cycle_partition)
    verify_published_diagnostics(store, root, receipt)
    started = monotonic()
    now_s = time.time() if now_unix_s is None else now_unix_s
    if not math.isfinite(now_s) or not math.isfinite(lease_seconds) or lease_seconds <= 0:
        raise ValueError("Dispatch requires a finite clock and a positive finite lease")
    key = dispatch_receipt_key(receipt, destination_id)
    identity = {
        "idempotency_key": dispatch_idempotency_key(receipt, destination_id),
        "model_id": receipt.model_id,
        "cycle_partition": receipt.cycle_partition,
        "attempt_id": receipt.attempt_id,
    }
    claim_tok = _claim_dispatch_slot(store, key, identity, now_s, lease_seconds)
    if claim_tok is None:
        return False
    if monotonic() - started >= lease_seconds:
        raise RuntimeError("Dispatch lease expired while claiming delivery; retry reconcile")
    try:
        status_code = send_webhook(receipt)
        if not (200 <= status_code < 300):
            raise RuntimeError(
                f"Consumer webhook rejected {receipt.cycle_partition}: {status_code}"
            )
    except Exception:
        failed_doc = json_bytes({"state": "failed", **identity})
        with contextlib.suppress(CASConflict):
            store.compare_and_swap(key, failed_doc, claim_tok)
        raise

    delivered_doc = json_bytes(
        {
            "state": "delivered",
            **identity,
            "publish_origin_utc": receipt.publish_origin_utc,
            "status_code": status_code,
        }
    )
    try:
        store.compare_and_swap(key, delivered_doc, claim_tok)
    except CASConflict:
        if store.get_bytes(key) != delivered_doc:
            raise
    return True


__all__ = [
    "dispatch_idempotency_key",
    "dispatch_receipt_key",
    "post_webhook_json",
    "reconcile_dispatch",
]
