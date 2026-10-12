"""
Application settings — loaded from environment variables via Pydantic Settings.
Never import os.environ directly; always use `get_settings()`.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Application ──────────────────────────────────────────────────────────
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    secret_key: str = Field(
        default="crm-development-secret-key-at-least-32-chars-long!",
        min_length=32,
    )
    app_encryption_key: str = Field(
        default="crm-development-encryption-key-32-chars!",
        min_length=32,
    )

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://postgres:postgres@localhost:5432/crm_dev"
    database_url_sync: str = "postgresql+psycopg2://postgres:postgres@localhost:5432/crm_dev"
    db_pool_size: int = 10
    db_max_overflow: int = 20
    db_pool_timeout: int = 30

    # ── Redis ─────────────────────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── JWT ───────────────────────────────────────────────────────────────────
    jwt_private_key_path: str = "./keys/private.pem"
    jwt_public_key_path: str = "./keys/public.pem"
    jwt_access_token_expire_minutes: int = 15
    jwt_refresh_token_expire_days: int = 7
    jwt_algorithm: str = "RS256"

    # ── LLM ───────────────────────────────────────────────────────────────────
    llm_provider: Literal["openai", "gemini", "ollama", "mock"] = "mock"
    openai_api_key: str = ""
    openai_model: str = "gpt-4o"
    llm_temperature: float = 0.1
    llm_max_tokens: int = 1000

    # ── WhatsApp ──────────────────────────────────────────────────────────────
    whatsapp_app_secret: str = ""
    whatsapp_verify_token: str = ""
    whatsapp_api_version: str = "v19.0"
    whatsapp_base_url: str = "https://graph.facebook.com"

    # ── Email ─────────────────────────────────────────────────────────────────
    smtp_host: str = "localhost"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "noreply@example.com"
    smtp_use_tls: bool = True

    # ── CORS ──────────────────────────────────────────────────────────────────
    allowed_origins: list[str] | str = ["http://localhost:5173", "http://localhost:3000"]

    @field_validator("allowed_origins", mode="before")
    @classmethod
    def parse_origins(cls, v: str | list[str]) -> list[str]:
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    # ── Celery ────────────────────────────────────────────────────────────────
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # ── Outbox ────────────────────────────────────────────────────────────────
    outbox_poll_interval_seconds: int = 30
    outbox_max_retries: int = 3

    # ── Lead Automation ───────────────────────────────────────────────────────
    default_stall_threshold_days: int = 7

    # ── File Storage ──────────────────────────────────────────────────────────
    s3_endpoint_url: str = ""
    s3_access_key_id: str = ""
    s3_secret_access_key: str = ""
    s3_bucket_name: str = "crm-property-photos"
    s3_public_url: str = ""

    # ── Google Sheets & OAuth ──────────────────────────────────────────────────
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8000/api/v1/integrations/google/callback"
    google_oauth_state_secret: str = ""
    google_sheets_credentials_json: str = ""
    google_sheets_spreadsheet_id: str = ""
    google_sheets_mode: Literal["real", "mock"] = "mock"

    # ── Sentry ────────────────────────────────────────────────────────────────
    sentry_dsn: str = ""

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def is_development(self) -> bool:
        return self.environment == "development"


@lru_cache
def get_settings() -> Settings:
    """Cached singleton — call get_settings() everywhere, do not instantiate Settings() directly."""
    return Settings()
