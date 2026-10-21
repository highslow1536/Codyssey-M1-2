"""Shared Discord sender loop for briefings and alerts."""

from types import SimpleNamespace

from app.errors import AppError
from app.providers.discord import DeliveryError, DiscordProvider


def drain_outbox(repository, uid, settings, clock, discord=None, part_limit=30):
    counts = {"parts_sent": 0, "delivery_errors": 0}
    for _ in range(part_limit):
        rows = list(repository.ready(uid, clock(), part_limit))
        claim = None
        for row in rows:
            claim = repository.claim_part(uid, row.id, clock())
            if claim:
                break
        if not claim:
            break
        result = None
        error = None
        try:
            provider = discord or DiscordProvider(settings.discord_webhook_url)
            result = provider.send(claim["content"])
        except DeliveryError as exc:
            error = exc
        except AppError as exc:
            error = SimpleNamespace(
                code=exc.code,
                message=exc.message,
                retryable=False,
                retry_after=None,
                uncertain=False,
            )
        except Exception:
            error = SimpleNamespace(
                code="discord_unavailable",
                message="전송 결과를 확인하지 못했습니다.",
                retryable=True,
                retry_after=None,
                uncertain=True,
            )
        saved = repository.finish_part(claim, clock(), result=result, error=error)
        if saved:
            counts["parts_sent" if result else "delivery_errors"] += 1
    return counts
