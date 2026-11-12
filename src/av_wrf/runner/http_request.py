"""Bounded retries for idempotent HTTP acquisition requests."""

from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import sleep

import httpx
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_random_exponential,
)

_MAX_WAIT = 60.0
_backoff = wait_random_exponential(multiplier=1, max=_MAX_WAIT)


def _transient(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or 500 <= exc.response.status_code < 600
    return isinstance(exc, (httpx.NetworkError, httpx.TimeoutException, httpx.RemoteProtocolError))


def _wait(state: RetryCallState) -> float:
    delay = _backoff(state)
    exc = state.outcome.exception() if state.outcome is not None else None
    if not isinstance(exc, httpx.HTTPStatusError):
        return delay
    header = exc.response.headers.get("retry-after", "").strip()
    if header.isascii() and header.isdecimal():
        seconds = _MAX_WAIT if len(header) > 9 else float(int(header))
    else:
        try:
            at = parsedate_to_datetime(header)
            seconds = (at - datetime.now(UTC)).total_seconds() if at.tzinfo else 0.0
        except (ValueError, TypeError, OverflowError):
            seconds = 0.0
    return min(_MAX_WAIT, max(delay, seconds))


def _request(
    client: httpx.Client,
    method: str,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, str] | None = None,
    after_attempt: Callable[[], None] | None = None,
) -> httpx.Response:
    if method not in ("GET", "HEAD"):
        raise ValueError("Only GET and HEAD requests are retryable")

    def send() -> httpx.Response:
        try:
            response = client.request(method, url, headers=headers, params=params)
            response.raise_for_status()
            return response
        finally:
            if after_attempt is not None:
                after_attempt()

    return Retrying(
        retry=retry_if_exception(_transient),
        stop=stop_after_attempt(3),
        wait=_wait,
        sleep=sleep,
        reraise=True,
    )(send)


__all__: list[str] = []
