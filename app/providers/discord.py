"""Discord webhook delivery: bounded, no redirects, token-safe errors."""

from __future__ import annotations

import math
import re
from urllib.parse import urlsplit

import httpx

from app.errors import AppError

REQUEST_TIMEOUT_S = 10.0
MAX_CONTENT_UNITS = 2000
MAX_RETRY_AFTER_S = 86400.0
ALLOWED_HOSTS = frozenset(
    {
        "discord.com",
        "ptb.discord.com",
        "canary.discord.com",
        "discordapp.com",
        "ptb.discordapp.com",
        "canary.discordapp.com",
    }
)

__all__ = ["DeliveryError", "DiscordProvider", "split_content"]


class DeliveryError(Exception):
    """Delivery failure. Main service persists destination cooldown on this."""

    def __init__(
        self,
        code: str,
        message: str,
        retryable: bool,
        retry_after: float | None = None,
        uncertain: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after
        self.uncertain = uncertain


def _utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _validate_webhook_url(url: object) -> str:
    if not isinstance(url, str) or not url.strip():
        raise AppError(503, "discord_not_configured", "Discord is not configured.")
    raw = url.strip()
    try:
        parts = urlsplit(raw)
    except Exception as exc:
        raise AppError(422, "discord_invalid_webhook", "Webhook URL invalid.") from exc
    if parts.scheme.lower() != "https":
        raise AppError(422, "discord_invalid_webhook", "Webhook URL invalid.")
    if parts.username or parts.password:
        raise AppError(422, "discord_invalid_webhook", "Webhook URL invalid.")
    try:
        host = (parts.hostname or "").lower()
        port = parts.port
    except Exception as exc:
        raise AppError(422, "discord_invalid_webhook", "Webhook URL invalid.") from exc
    if host not in ALLOWED_HOSTS or port is not None:
        raise AppError(422, "discord_invalid_webhook", "Webhook URL invalid.")
    if parts.query or parts.fragment:
        raise AppError(422, "discord_invalid_webhook", "Webhook URL invalid.")
    segments = [seg for seg in parts.path.split("/") if seg]
    if len(segments) == 4 and segments[0] == "api" and segments[1] == "webhooks":
        _id, token = segments[2], segments[3]
    elif len(segments) == 5 and segments[0] == "api" and segments[2] == "webhooks":
        if segments[1] != "v10":
            raise AppError(422, "discord_invalid_webhook", "Webhook URL invalid.")
        _id, token = segments[3], segments[4]
    else:
        raise AppError(422, "discord_invalid_webhook", "Webhook URL invalid.")
    if re.fullmatch(r"[0-9]+", _id) is None:
        raise AppError(422, "discord_invalid_webhook", "Webhook URL invalid.")
    if re.fullmatch(r"[A-Za-z0-9_-]+", token) is None:
        raise AppError(422, "discord_invalid_webhook", "Webhook URL invalid.")
    return "https://" + host + "/" + "/".join(segments)


def _validate_content(content: object) -> str:
    if not isinstance(content, str):
        raise AppError(422, "discord_invalid_content", "Content must be text.")
    if not content.strip():
        raise AppError(422, "discord_invalid_content", "Content must not be empty.")
    if len(content) > MAX_CONTENT_UNITS or _utf16_len(content) > MAX_CONTENT_UNITS:
        raise AppError(422, "discord_invalid_content", "Content exceeds 2000 chars.")
    return content


def _parse_retry_after_seconds(resp: httpx.Response) -> float | None:
    candidates: list[float] = []
    header = resp.headers.get("retry-after")
    if header is not None:
        try:
            value = float(str(header).strip())
        except (TypeError, ValueError):
            value = math.nan
        if math.isfinite(value) and value >= 0:
            candidates.append(value)
    try:
        body = resp.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        raw = body.get("retry_after")
        if isinstance(raw, bool):
            raw = None
        if isinstance(raw, (int, float)):
            number = float(raw)
            if math.isfinite(number) and number >= 0:
                candidates.append(number)
    if not candidates:
        return None
    return min(max(candidates), MAX_RETRY_AFTER_S)


class DiscordProvider:
    """POST-only Discord webhook sender over httpx (tests inject transport)."""

    def __init__(
        self, webhook_url: str | None = None, *, transport: httpx.BaseTransport | None = None
    ) -> None:
        if webhook_url is None:
            self._webhook_url: str | None = None
        else:
            self._webhook_url = _validate_webhook_url(webhook_url)
        self._transport = transport

    def _client(self) -> httpx.Client:
        return httpx.Client(transport=self._transport, follow_redirects=False)

    def send(self, content: str) -> dict:
        """Send content; return {'message_id': str} from wait=true response."""
        if self._webhook_url is None:
            raise AppError(503, "discord_not_configured", "Discord not configured.")
        body = _validate_content(content)
        try:
            with self._client() as client:
                resp = client.post(
                    self._webhook_url,
                    params={"wait": "true"},
                    json={"content": body, "allowed_mentions": {"parse": []}, "flags": 4},
                    timeout=REQUEST_TIMEOUT_S,
                )
        except httpx.TimeoutException as exc:
            raise DeliveryError(
                "discord_timeout", "Discord delivery timed out.", True, None, True
            ) from exc
        except httpx.RequestError as exc:
            raise DeliveryError(
                "discord_network_error", "Discord delivery failed.", True, None, True
            ) from exc
        return self._handle_response(resp)

    def _handle_response(self, resp: httpx.Response) -> dict:
        status = resp.status_code
        if 200 <= status <= 299:
            try:
                data = resp.json()
            except Exception as exc:
                raise DeliveryError(
                    "discord_bad_response", "Discord response unreadable.", False, None, True
                ) from exc
            msg_id = data.get("id") if isinstance(data, dict) else None
            if isinstance(msg_id, bool) or not isinstance(msg_id, (str, int)):
                raise DeliveryError(
                    "discord_bad_response", "Discord response unreadable.", False, None, True
                )
            text_id = str(msg_id).strip()
            if re.fullmatch(r"[0-9]+", text_id) is None:
                raise DeliveryError(
                    "discord_bad_response", "Discord response unreadable.", False, None, True
                )
            return {"message_id": text_id}
        if status == 429:
            raise DeliveryError(
                "discord_rate_limited",
                "Discord rate limit hit.",
                True,
                _parse_retry_after_seconds(resp),
                False,
            )
        if status == 401:
            raise DeliveryError("discord_unauthorized", "Discord delivery refused (401).", False)
        if status == 403:
            raise DeliveryError("discord_forbidden", "Discord delivery refused (403).", False)
        if status == 404:
            raise DeliveryError("discord_unknown_webhook", "Discord delivery refused (404).", False)
        if 300 <= status <= 399:
            raise DeliveryError(
                "discord_unexpected_redirect", "Discord redirect refused.", False, None, False
            )
        if 500 <= status <= 599:
            raise DeliveryError(
                "discord_server_error", "Discord delivery failed.", True, None, True
            )
        raise DeliveryError("discord_rejected", "Discord delivery refused.", False, None, False)


def split_content(
    text: str, event_id: object, max_length: int = 1800, kind: str = "briefing"
) -> list[str]:
    """Split text into stable prefixed parts, each <=2000 UTF-16 units."""
    if not isinstance(text, str):
        raise AppError(422, "discord_invalid_content", "Content must be text.")
    if isinstance(max_length, bool) or not isinstance(max_length, int):
        raise AppError(422, "discord_invalid_content", "max_length invalid.")
    if max_length <= 0:
        raise AppError(422, "discord_invalid_content", "max_length invalid.")
    if kind not in ("briefing", "alert"):
        raise AppError(422, "discord_invalid_content", "kind invalid.")
    eid = str(event_id) if isinstance(event_id, (str, int)) else None
    if eid is None or not eid.strip():
        raise AppError(422, "discord_invalid_content", "event_id required.")
    eid = eid.strip()
    total = 1
    while True:
        longest = _utf16_len("[" + kind + " " + eid + " " + str(total))
        longest += _utf16_len("/" + str(total) + "] ")
        budget = min(max_length, MAX_CONTENT_UNITS - longest)
        if budget <= 0:
            raise AppError(422, "discord_invalid_content", "Prefix too long.")
        need = _parts_needed(text, budget)
        if need == total:
            break
        total = need
        if total > 100000:
            raise AppError(422, "discord_invalid_content", "Content too long.")
    out: list[str] = []
    widths = _utf16_widths(text)
    start = 0
    budget = min(max_length, MAX_CONTENT_UNITS - _prefix_len(eid, total, total, kind))
    for index in range(1, total + 1):
        prefix = "[" + kind + " " + eid + " " + str(index) + "/" + str(total) + "] "
        end = _chunk_end(text, widths, start, budget)
        out.append(prefix + text[start:end])
        start = end
    return out


def _prefix_len(eid: str, index: int, total: int, kind: str = "briefing") -> int:
    return _utf16_len("[" + kind + " " + eid + " " + str(index) + "/" + str(total) + "] ")


def _utf16_widths(text: str) -> list[int]:
    return [2 if ord(ch) > 0xFFFF else 1 for ch in text]


def _parts_needed(text: str, budget: int) -> int:
    widths = _utf16_widths(text)
    count, start = 0, 0
    while start < len(text):
        start = _chunk_end(text, widths, start, budget)
        count += 1
    return max(1, count)


def _chunk_end(text: str, widths: list[int], start: int, budget: int) -> int:
    end = _advance(widths, start, budget)
    if end < len(text):
        boundary = text.rfind("\n", start, end)
        if boundary >= start:
            return boundary + 1
    return end


def _advance(widths: list[int], start: int, budget: int) -> int:
    used = 0
    pos = start
    while pos < len(widths) and used + widths[pos] <= budget:
        used += widths[pos]
        pos += 1
    if pos == start and start < len(widths):
        raise AppError(422, "discord_invalid_content", "Character exceeds part budget.")
    return pos
