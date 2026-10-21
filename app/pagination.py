import base64
import hashlib
import json

from app.errors import AppError


def scope_key(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, default=str).encode()).hexdigest()[:24]


def encode_cursor(scope: str, values: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps({"scope": scope, **values}).encode()).decode()


def decode_cursor(cursor: str, scope: str) -> dict:
    try:
        if len(cursor) > 2048:
            raise ValueError
        value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(value, dict) or value.pop("scope") != scope:
            raise ValueError
        return value
    except (ValueError, KeyError, TypeError) as exc:
        raise AppError(422, "invalid_cursor", "조회 조건에 맞는 cursor가 필요합니다.") from exc
