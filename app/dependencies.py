from typing import Annotated

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from firebase_admin import auth
from google.auth.exceptions import TransportError

from app.config import get_settings
from app.errors import AppError
from app.firebase import get_firebase_app, get_firestore

bearer = HTTPBearer(auto_error=False)


def get_current_uid(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> str:
    if not credentials or credentials.scheme.lower() != "bearer":
        raise AppError(401, "unauthenticated", "Firebase ID token이 필요합니다.")
    settings = get_settings()
    if not settings.allowed_uids:
        raise AppError(503, "configuration_missing", "ALLOWED_USER_UIDS를 설정하세요.")
    try:
        claims = auth.verify_id_token(credentials.credentials, app=get_firebase_app())
    except AppError:
        raise
    except (auth.CertificateFetchError, TransportError) as exc:
        raise AppError(
            503, "auth_unavailable", "Firebase 인증 서비스에 연결할 수 없습니다."
        ) from exc
    except (ValueError, auth.InvalidIdTokenError) as exc:
        raise AppError(401, "invalid_token", "유효한 Firebase ID token이 필요합니다.") from exc
    uid = claims.get("uid", "")
    if uid not in settings.allowed_uids:
        raise AppError(403, "forbidden", "허용된 사용자만 사용할 수 있습니다.")
    return uid


def get_db(uid: Annotated[str, Depends(get_current_uid)]):
    # Resolve authentication before opening the DB, independent of route argument order.
    return get_firestore()
