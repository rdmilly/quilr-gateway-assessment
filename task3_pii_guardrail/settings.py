"""Settings for the PII guardrail endpoint, read from the [llm_gateway] section."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import Field, field_validator

from shared.config import Section


class LlmGatewaySettings(Section):
    # The one provider this endpoint calls. Fixed here, never taken from a request.
    provider_url: str = "http://127.0.0.1:9100/v1/chat/completions"
    # Name of the environment variable holding the provider's API key, if it needs one.
    # The key itself never goes in the config file, and the caller's key is never forwarded.
    provider_api_key_env: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]*$")
    max_body_bytes: int = Field(default=1_048_576, gt=0, le=16 * 1_048_576)
    max_response_bytes: int = Field(default=8 * 1_048_576, gt=0, le=64 * 1_048_576)
    upstream_timeout_s: float = Field(default=60.0, gt=0, le=600)
    host: str = "127.0.0.1"
    port: int = Field(default=8081, ge=1, le=65535)

    @field_validator("provider_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("must be an http or https URL with a host")
        return value
