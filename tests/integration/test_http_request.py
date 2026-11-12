"""Exercise bounded retries without delaying the test suite."""

from unittest.mock import patch

import httpx
import pytest

from av_wrf.runner.http_request import _request


@pytest.mark.parametrize("status", [404, 412])
def test_permanent_status_is_not_retried(status: int) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status)

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        patch("av_wrf.runner.http_request.sleep") as sleep,
        pytest.raises(httpx.HTTPStatusError),
    ):
        _request(client, "GET", "https://example.invalid")
    assert len(calls) == 1
    sleep.assert_not_called()


@pytest.mark.parametrize("header", ["999999999999999999999999", "Fri, 31 Dec 9999 23:59:59 GMT"])
def test_retry_after_is_capped_and_attempts_are_bounded(header: str) -> None:
    calls = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": header})

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        patch("av_wrf.runner.http_request.sleep") as sleep,
        pytest.raises(httpx.HTTPStatusError),
    ):
        _request(client, "HEAD", "https://example.invalid")
    assert len(calls) == 3
    assert [call.args[0] for call in sleep.call_args_list] == [60.0, 60.0]


def test_transport_retry_and_unsafe_method_rejection() -> None:
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(200)

    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as client,
        patch("av_wrf.runner.http_request.sleep") as sleep,
    ):
        assert _request(client, "GET", "https://example.invalid").status_code == 200
        with pytest.raises(ValueError, match="Only GET and HEAD"):
            _request(client, "POST", "https://example.invalid")
    assert calls == 3
    assert sleep.call_count == 2
