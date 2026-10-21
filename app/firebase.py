import json
import os
from functools import lru_cache

import firebase_admin
from firebase_admin import credentials, firestore
from google.auth.credentials import AnonymousCredentials
from google.cloud.firestore_v1 import Client

from app.config import get_settings
from app.errors import AppError


@lru_cache
def get_firebase_app():
    settings = get_settings()
    if not settings.firebase_project_id:
        raise AppError(503, "configuration_missing", "FIREBASE_PROJECT_ID를 설정하세요.")
    try:
        if settings.firebase_auth_emulator_host:
            os.environ["FIREBASE_AUTH_EMULATOR_HOST"] = settings.firebase_auth_emulator_host
        if settings.firebase_service_account_json:
            cred = credentials.Certificate(
                json.loads(settings.firebase_service_account_json.get_secret_value())
            )
        elif settings.google_application_credentials:
            cred = credentials.Certificate(settings.google_application_credentials)
        elif settings.firestore_emulator_host and settings.firebase_auth_emulator_host:
            # Authentication emulator verifies its own unsigned test tokens only locally.
            class EmulatorCredential(credentials.Base):
                def get_credential(self):
                    return AnonymousCredentials()

            cred = EmulatorCredential()
        else:
            cred = credentials.ApplicationDefault()
        return firebase_admin.initialize_app(
            cred, {"projectId": settings.firebase_project_id}, name="stock-assistant"
        )
    except (ValueError, OSError, KeyError, json.JSONDecodeError) as exc:
        raise AppError(
            503, "configuration_invalid", "Firebase 서버 자격 설정을 확인하세요."
        ) from exc


@lru_cache
def get_firestore() -> Client:
    settings = get_settings()
    if not settings.firebase_project_id:
        raise AppError(503, "configuration_missing", "FIREBASE_PROJECT_ID를 설정하세요.")
    if settings.firestore_emulator_host:
        os.environ["FIRESTORE_EMULATOR_HOST"] = settings.firestore_emulator_host
        return Client(project=settings.firebase_project_id, credentials=AnonymousCredentials())
    try:
        return firestore.client(app=get_firebase_app())
    except AppError:
        raise
    except Exception as exc:
        raise AppError(
            503, "firestore_unavailable", "Firestore 자격과 연결 설정을 확인하세요."
        ) from exc
