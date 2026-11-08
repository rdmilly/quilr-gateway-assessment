"""Settings for the rate-limited model router, read from the [llm_router] section."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import Field, field_validator

from shared.config import Section

_ENV_NAME = r"^[A-Z][A-Z0-9_]*$"


class RouterSettings(Section):
    # The two providers, fixed here and never taken from a request.
    primary_url: str = "http://127.0.0.1:9100/v1/chat/completions"
    backup_url: str = "http://127.0.0.1:9101/v1/chat/completions"
    # Names of environment variables holding each provider's key. Keys never go in the file,
    # and the caller's own key is never forwarded to a provider.
    primary_api_key_env: str | None = Field(default=None, pattern=_ENV_NAME)
    backup_api_key_env: str | None = Field(default=None, pattern=_ENV_NAME)

    # Fail over when the primary answers 429 or has not answered within this time. For a
    # stream, "answered" means its first chunk arrived; for a plain request, the whole body.
    failover_timeout_ms: int = Field(default=3000, gt=0, le=600_000)
    # The brief names 429 and timeouts. Set this to also fail over on 5xx and connection errors.
    failover_on_errors: bool = False
    # Once a stream has started it cannot move to the backup; this cuts one that goes silent.
    stream_idle_timeout_s: float = Field(default=60.0, gt=0, le=3600)

    tokens_per_window: int = Field(default=50_000, ge=1)
    window_s: float = Field(default=60.0, gt=0, le=86_400)
    db_path: str = "data/rate_limits.sqlite3"
    # Completion tokens assumed when a request sets no max_tokens.
    default_max_tokens: int = Field(default=1024, ge=1)

    max_body_bytes: int = Field(default=1_048_576, gt=0, le=16 * 1_048_576)
    max_response_bytes: int = Field(default=8 * 1_048_576, gt=0, le=64 * 1_048_576)
    host: str = "127.0.0.1"
    port: int = Field(default=8082, ge=1, le=65535)

    @field_validator("primary_url", "backup_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("must be an http or https URL with a host")
        return value
