import logging
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from google.api_core.exceptions import GoogleAPICallError

from app.api import alerts, briefings, chat, conversations, data, jobs, market, research
from app.config import get_settings
from app.errors import AppError

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title="주식시장 AI 비서",
        version="0.1.0",
        description="실제 시장 데이터·과제 데이터 CRUD·대화·요약 기반 채팅 API",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=settings.origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["X-Request-ID"],
    )

    @application.middleware("http")
    async def request_id(request: Request, call_next):
        request.state.request_id = uuid4().hex
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def error(request, status, code, message, details=None):
        content = {
            "code": code,
            "message": message,
            "request_id": getattr(request.state, "request_id", None),
        }
        if details is not None:
            content["detail"] = details
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        return JSONResponse(status_code=status, content=content, headers=headers)

    @application.exception_handler(AppError)
    async def app_error(request, exc):
        return error(request, exc.status_code, exc.code, exc.message)

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Avoid serializing arbitrary exception objects carried in Pydantic ctx.
        details = [
            {"loc": item["loc"], "type": item["type"], "msg": item["msg"]} for item in exc.errors()
        ]
        return error(request, 422, "validation_error", "요청 항목을 확인하세요.", details)

    @application.exception_handler(HTTPException)
    async def http_error(request, exc):
        return error(request, exc.status_code, "http_error", str(exc.detail))

    @application.exception_handler(GoogleAPICallError)
    async def database_error(request, exc):
        logger.error(
            "Firestore request failed: %s request_id=%s",
            type(exc).__name__,
            getattr(request.state, "request_id", None),
        )
        return error(
            request, 503, "firestore_unavailable", "Firestore 연결과 인덱스 설정을 확인하세요."
        )

    @application.get("/healthz", tags=["health"])
    def health():
        # Liveness only: no secrets or misleading dependency-readiness claim.
        return {"status": "ok"}

    application.include_router(data.router)
    application.include_router(conversations.router)
    application.include_router(chat.router)
    application.include_router(market.router)
    application.include_router(jobs.router)
    application.include_router(research.router)
    application.include_router(briefings.router)
    application.include_router(alerts.router)
    return application


app = create_app()
