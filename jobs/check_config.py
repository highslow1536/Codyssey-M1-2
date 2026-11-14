"""Offline deployment preflight. Never contacts Firebase, providers, or Discord."""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit

from firebase_admin import credentials
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.errors import AppError
from app.providers.discord import DiscordProvider
from app.providers.openai_client import model_key


def is_public_https_origin(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname
            and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
            and not parsed.hostname.endswith(".localhost")
            and not any(c.isspace() or c in "*?#" for c in value)
            and parsed.username is None
            and parsed.password is None
            and not parsed.netloc.endswith(":")
            and parsed.path == ""
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError:
        return False


def check_config(settings: Settings, component: str) -> dict:
    """Return only stable codes, never credential contents or arbitrary exceptions."""
    errors, warnings = [], []
    if settings.app_env != "production":
        errors.append("APP_ENV_must_be_production")
    if not settings.firebase_project_id:
        errors.append("FIREBASE_PROJECT_ID_required")
    if not settings.allowed_uids:
        errors.append("ALLOWED_USER_UIDS_required")

    if not (settings.firebase_service_account_json or settings.google_application_credentials):
        errors.append("Firebase_service_account_required")
    else:
        try:
            raw = (
                settings.firebase_service_account_json.get_secret_value()
                if settings.firebase_service_account_json
                else Path(settings.google_application_credentials).read_text(encoding="utf-8")
            )
            certificate = credentials.Certificate(json.loads(raw))
            if certificate.project_id != settings.firebase_project_id:
                errors.append("Firebase_service_account_project_mismatch")
        except (ValueError, TypeError, OSError, KeyError):
            errors.append("Firebase_service_account_invalid")

    if component == "api":
        if not settings.origins or not all(map(is_public_https_origin, settings.origins)):
            errors.append("ALLOWED_ORIGINS_must_be_exact_public_https_origins")
        if bool(settings.job_owner_uid) != bool(settings.discord_webhook_url):
            warnings.append("Discord_queue_and_worker_require_both_JOB_OWNER_UID_and_webhook")
    elif component == "worker":
        if not settings.job_owner_uid or settings.job_owner_uid not in settings.allowed_uids:
            errors.append("JOB_OWNER_UID_must_be_allowlisted")
        try:
            if not settings.discord_webhook_url:
                raise AppError(503, "discord_not_configured", "")
            DiscordProvider(settings.discord_webhook_url)  # Validates locally; does not send.
        except AppError:
            errors.append("DISCORD_WEBHOOK_URL_required_or_invalid")
    else:
        raise ValueError("Unknown component")

    if not (model_key(settings) and settings.openai_model):
        warnings.append("OpenAI_chat_unavailable_news_uses_excerpts")
    if not (settings.naver_client_id and settings.naver_client_secret):
        warnings.append("Naver_news_unavailable")
    if not settings.finnhub_api_key:
        warnings.append("Finnhub_news_unavailable")
    return {"component": component, "ok": not errors, "errors": errors, "warnings": warnings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--component", choices=("api", "worker"), required=True)
    args = parser.parse_args()
    try:
        result = check_config(get_settings(), args.component)
    except ValidationError:
        # Pydantic errors can include input/context. Do not serialize them here.
        result = {
            "component": args.component,
            "ok": False,
            "errors": ["Invalid_settings_check_types_ranges_and_production_emulator_ban"],
            "warnings": [],
        }
    print(json.dumps(result), flush=True)
    if not result["ok"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
