"""Gateway settings, read from the [mcp_gateway] section of the config file."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import Field, field_validator

from shared.config import Section


class GatewaySettings(Section):
    # The one server this gateway forwards to. Fixed here, never taken from a
    # request, so a caller cannot point the gateway at another host (SSRF).
    downstream_url: str = "http://127.0.0.1:9000/mcp"
    max_body_bytes: int = Field(default=1_048_576, gt=0, le=16 * 1_048_576)
    upstream_timeout_s: float = Field(default=30.0, gt=0, le=300)
    # Browsers send Origin; agents usually don't. A request that carries an Origin
    # not listed here is refused (DNS rebinding protection, required by the spec).
    allowed_origins: tuple[str, ...] = ()
    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)

    @field_validator("downstream_url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("must be an http or https URL with a host")
        return value
