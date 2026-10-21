from functools import lru_cache

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", hide_input_in_errors=True
    )

    app_env: str = "development"
    firebase_project_id: str | None = None
    firebase_service_account_json: SecretStr | None = None
    google_application_credentials: str | None = None
    firestore_emulator_host: str | None = None
    firebase_auth_emulator_host: str | None = None
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    allowed_user_uids: str = ""
    job_owner_uid: str | None = None
    mcp_owner_uid: str | None = None
    openai_api_key: str | None = Field(default=None, repr=False)
    openai_model: str | None = None
    openai_max_output_tokens: int = Field(default=1200, ge=100, le=8000)
    max_tool_calls: int = Field(default=5, ge=0, le=10)
    max_tool_rounds: int = Field(default=3, ge=1, le=5)
    chat_lease_seconds: int = Field(default=300, ge=60, le=900)
    naver_client_id: str | None = Field(default=None, repr=False)
    naver_client_secret: str | None = Field(default=None, repr=False)
    finnhub_api_key: str | None = Field(default=None, repr=False)
    discord_webhook_url: str | None = Field(default=None, repr=False)
    data_poll_minutes: int = Field(default=5, ge=1, le=60)
    news_cache_minutes: int = Field(default=30, ge=1)
    sentiment_cache_minutes: int = Field(default=30, ge=1)
    analysis_cache_minutes: int = Field(default=30, ge=1)

    @field_validator(
        "firebase_project_id",
        "firebase_service_account_json",
        "google_application_credentials",
        "firestore_emulator_host",
        "firebase_auth_emulator_host",
        "job_owner_uid",
        "mcp_owner_uid",
        "openai_api_key",
        "openai_model",
        "naver_client_id",
        "naver_client_secret",
        "finnhub_api_key",
        "discord_webhook_url",
        mode="before",
    )
    @classmethod
    def empty_to_none(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

    @model_validator(mode="after")
    def check_emulators(self):
        if self.app_env not in {"development", "test", "production"}:
            raise ValueError("APP_ENV must be development, test or production")
        if self.app_env == "production" and (
            self.firestore_emulator_host or self.firebase_auth_emulator_host
        ):
            raise ValueError("Emulators are forbidden in production")
        if self.firebase_service_account_json and self.google_application_credentials:
            raise ValueError("Choose one Firebase service-account credential source")
        return self

    @property
    def origins(self) -> list[str]:
        return [item.strip() for item in self.allowed_origins.split(",") if item.strip()]

    @property
    def allowed_uids(self) -> set[str]:
        return {item.strip() for item in self.allowed_user_uids.split(",") if item.strip()}


@lru_cache
def get_settings() -> Settings:
    return Settings()
