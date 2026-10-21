"""Discord provider tests: security, rate limits, splitting (mock transport only)."""

from __future__ import annotations

import json

import httpx
import pytest

from app.errors import AppError
from app.providers.discord import DeliveryError, DiscordProvider, split_content

URL = "https://discord.com/api/webhooks/123456789/AbC123_-xYz"
TOKEN = "AbC123_-xYz"


def _provider(handler) -> DiscordProvider:
    return DiscordProvider(URL, transport=httpx.MockTransport(handler))


def test_send_success_returns_real_message_id():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["wait"] = request.url.params.get("wait")
        seen["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"id": "1199887766", "content": "hi"})

    result = _provider(handler).send("hello")
    assert result == {"message_id": "1199887766"}
    assert isinstance(result["message_id"], str)
    assert seen["wait"] == "true"
    assert seen["body"]["allowed_mentions"] == {"parse": []}
    assert seen["body"]["content"] == "hello"


def test_send_success_v10_webhook_path():
    url = "https://discord.com/api/v10/webhooks/123456789/AbC123_-xYz"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.startswith("/api/v10/webhooks/")
        return httpx.Response(200, json={"id": "42"})

    provider = DiscordProvider(url, transport=httpx.MockTransport(handler))
    assert provider.send("hi") == {"message_id": "42"}


def test_missing_config_is_503():
    with pytest.raises(AppError) as exc:
        DiscordProvider(None).send("hi")
    assert exc.value.status_code == 503
    assert exc.value.code == "discord_not_configured"


@pytest.mark.parametrize(
    "bad",
    [
        "http://discord.com/api/webhooks/123/AbC123_-xYz",
        "https://evil.example.com/api/webhooks/123/AbC123_-xYz",
        "https://discord.com/api/webhooks/123/" + TOKEN + "?wait=true",
        "https://discord.com/api/webhooks/123/" + TOKEN + "#frag",
        "https://user:pass@discord.com/api/webhooks/123/" + TOKEN,
        "https://discord.com:8443/api/webhooks/123/" + TOKEN,
        "https://discord.com/api/webhooks/abc/" + TOKEN,
        "https://discord.com/api/webhooks/123/" + TOKEN + "/extra",
        "https://discord.com/api/webhooks/123/bad%20token",
        "https://discord.com/api/webhooks/123/bad.token!x",
        "https://discord.com/api/channels/123",
    ],
)
def test_invalid_webhook_rejected_without_token_leak(bad):
    with pytest.raises(AppError) as exc:
        DiscordProvider(bad)
    assert TOKEN not in str(exc.value)
    assert TOKEN not in exc.value.message


def test_invalid_content_rejected():
    provider = _provider(lambda request: httpx.Response(200, json={"id": "1"}))
    with pytest.raises(AppError):
        provider.send("")
    with pytest.raises(AppError):
        provider.send("x" * 2001)


def test_rate_limited_header_and_json_retry_after():
    def header_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "120"}, json={})

    with pytest.raises(DeliveryError) as exc:
        _provider(header_handler).send("hi")
    assert exc.value.code == "discord_rate_limited"
    assert exc.value.retryable is True
    assert exc.value.retry_after == 120

    def json_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"retry_after": 7.5})

    with pytest.raises(DeliveryError) as exc:
        _provider(json_handler).send("hi")
    assert exc.value.code == "discord_rate_limited"
    assert exc.value.retry_after == 7.5


def test_rate_limited_retry_after_bounded():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "999999"}, json={})

    with pytest.raises(DeliveryError) as exc:
        _provider(handler).send("hi")
    assert exc.value.code == "discord_rate_limited"
    assert exc.value.retry_after == 86400


@pytest.mark.parametrize("status", [401, 403, 404])
def test_terminal_statuses_not_retryable(status):
    def handler(request: httpx.Request, status=status) -> httpx.Response:
        return httpx.Response(status, json={"message": "nope"})

    with pytest.raises(DeliveryError) as exc:
        _provider(handler).send("hi")
    assert exc.value.retryable is False
    assert exc.value.uncertain is False
    assert TOKEN not in str(exc.value)


def test_server_error_uncertain_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"message": "boom " + TOKEN})

    with pytest.raises(DeliveryError) as exc:
        _provider(handler).send("hi")
    assert exc.value.retryable is True
    assert exc.value.uncertain is True
    assert TOKEN not in str(exc.value)
    assert TOKEN not in exc.value.message


def test_timeout_uncertain_retryable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with pytest.raises(DeliveryError) as exc:
        _provider(handler).send("hi")
    assert exc.value.retryable is True
    assert exc.value.uncertain is True
    assert TOKEN not in str(exc.value)


def test_redirect_not_followed():
    seen: dict = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["count"] += 1
        return httpx.Response(307, headers={"location": "https://evil.example.com/x"})

    with pytest.raises(DeliveryError) as exc:
        _provider(handler).send("hi")
    assert exc.value.retryable is False
    assert seen["count"] == 1


def test_success_requires_digit_id():
    for payload in ({}, {"id": None}, {"id": {"nested": 1}}, {"id": "abc"}):

        def handler(request: httpx.Request, payload=payload) -> httpx.Response:
            return httpx.Response(200, json=payload)

        with pytest.raises(DeliveryError):
            _provider(handler).send("hi")


def _utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def test_split_content_roundtrip_and_prefix():
    text = "hello world " * 400
    parts = split_content(text, "E9")
    assert len(parts) > 1
    assert parts[0].startswith("[briefing E9 1/")
    assert "".join(p.split("] ", 1)[1] for p in parts) == text
    for index, part in enumerate(parts, 1):
        assert part.startswith("[briefing E9 " + str(index) + "/" + str(len(parts)) + "] ")
        assert _utf16_len(part) <= 2000


def test_split_content_conservative_astral_counting():
    text = "\U0001f600" * 1500
    parts = split_content(text, "EMOJI")
    assert len(parts) > 1
    for part in parts:
        assert _utf16_len(part) <= 2000
    bodies = "".join(p.split("] ", 1)[1] for p in parts)
    assert bodies == text


def test_split_content_invalid():
    with pytest.raises(AppError):
        split_content(123, "E1")
    with pytest.raises(AppError):
        split_content("hi", "")
    with pytest.raises(AppError):
        split_content("hi", "E1", max_length=0)


def test_split_keeps_source_urls_intact_when_lines_fit():
    url = "https://example.test/source/" + "a" * 600
    text = "관측 요약\n" * 240 + url + "\n최신 관측 기준입니다."
    parts = split_content(text, "SOURCE")
    assert any(url in part for part in parts)
    assert "".join(part.split("] ", 1)[1] for part in parts) == text
    assert all(_utf16_len(part) <= 2000 for part in parts)
