"""
foodpilot/config.py

Single source of truth for all environment variables.
Uses pydantic-settings to validate and parse from .env automatically.
"""
from functools import lru_cache

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # silently ignore unknown vars in .env
    )

    # ── Anthropic ─────────────────────────────────────────────────────────────
    anthropic_api_key: SecretStr

    # ── Supabase ──────────────────────────────────────────────────────────────
    supabase_url: str
    supabase_anon_key: SecretStr
    supabase_service_role_key: SecretStr
    supabase_jwt_secret: SecretStr

    # ── Token encryption (Fernet key, base64-encoded 32 bytes) ────────────────
    token_encryption_key: SecretStr

    # ── Swiggy OAuth ─────────────────────────────────────────────────────────
    swiggy_redirect_uri: str = "http://localhost:8000/auth/swiggy/callback"
    # After first /auth/swiggy/connect, copy the logged client_id here to skip DCR on restart
    swiggy_client_id: str | None = None

    # ── Supabase Auth callback (Google OAuth) ─────────────────────────────────
    # Must be allowlisted in Supabase dashboard → Auth → URL Configuration → Redirect URLs
    supabase_auth_redirect_url: str = "http://localhost:8000/auth/callback"



    # ── App ───────────────────────────────────────────────────────────────────
    log_level: str = "INFO"
    app_env: str = "development"
    sentry_dsn: str | None = None

    # ── Conversation settings ─────────────────────────────────────────────────
    # TTL: how long a conversation lives before pg_cron deletes it (hours)
    conversation_ttl_hours: int = 48

    # Context window: max messages sent to Claude per turn.
    # Strategy: keep first 2 messages (establish intent/address) + last N-2 recent.
    # Claude Sonnet 4.6 has 200k tokens; 10 messages keeps token costs very low.
    # Raise this if you notice Claude losing track of earlier instructions.
    max_conversation_history: int = 10

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"log_level must be one of {allowed}")
        return upper

    @field_validator("app_env")
    @classmethod
    def validate_app_env(cls, v: str) -> str:
        allowed = {"development", "staging", "production"}
        if v not in allowed:
            raise ValueError(f"app_env must be one of {allowed}")
        return v


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance. Call get_settings() everywhere — never instantiate Settings directly."""
    return Settings()
