"""Odolnost HTTP klienta – retry, detekce blokace, rotace identity."""

import httpx
import pytest

from vyhledavac.errors import BlockedError, TransientProviderError
from vyhledavac.http import HttpClient, RetryPolicy, build_headers, looks_blocked


def test_detects_cloudflare_page_behind_http_200():
    body = "<html><title>Just a moment...</title><div id='cf-browser-verification'></div></html>"
    assert looks_blocked(200, body)


def test_detects_403():
    assert looks_blocked(403, "")


def test_normal_json_is_not_blocked():
    assert not looks_blocked(200, '{"routes": []}')


def test_headers_are_internally_consistent():
    """Chrome UA musí mít i sec-ch-ua; Firefox naopak ne."""
    chrome = build_headers({
        "user_agent": "Mozilla/5.0 Chrome/131.0.0.0",
        "sec_ch_ua": '"Chrome";v="131"', "platform": '"Windows"'})
    assert "sec-ch-ua" in chrome and chrome["sec-ch-ua-mobile"] == "?0"

    firefox = build_headers({
        "user_agent": "Mozilla/5.0 Firefox/133.0", "sec_ch_ua": "", "platform": '"Windows"'})
    assert "sec-ch-ua" not in firefox


def test_retry_backoff_grows_and_is_capped():
    policy = RetryPolicy(base_delay=1.0, max_delay=8.0, jitter=0.0)
    assert policy.delay_for(0) == 1.0
    assert policy.delay_for(1) == 2.0
    assert policy.delay_for(10) == 8.0


@pytest.mark.asyncio
async def test_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503, text="upstream down")
        return httpx.Response(200, json={"ok": True})

    client = HttpClient("test", retry=RetryPolicy(attempts=4, base_delay=0.01, jitter=0))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        assert await client.get_json("https://example.test/api") == {"ok": True}
        assert calls["n"] == 3
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_block_raises_blocked_error_and_rotates_identity():
    seen_agents = []

    def handler(request):
        seen_agents.append(request.headers.get("user-agent"))
        return httpx.Response(403, text="Access denied")

    client = HttpClient("test", retry=RetryPolicy(attempts=3, base_delay=0.01, jitter=0))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(BlockedError):
            await client.get_json("https://example.test/api")
        assert len(seen_agents) == 3
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_html_instead_of_json_is_reported_as_block():
    def handler(request):
        return httpx.Response(200, text="<html><body>captcha required</body></html>")

    client = HttpClient("test", retry=RetryPolicy(attempts=1, base_delay=0.01, jitter=0))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(BlockedError):
            await client.get_json("https://example.test/api")
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_timeout_is_retried_then_reported():
    def handler(request):
        raise httpx.ConnectTimeout("too slow")

    client = HttpClient("test", retry=RetryPolicy(attempts=2, base_delay=0.01, jitter=0))
    client._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(TransientProviderError):
            await client.get_json("https://example.test/api")
    finally:
        await client.close()
