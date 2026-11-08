"""``python -m task2_mcp_gateway`` runs the gateway, reading config.toml (or $GATEWAY_CONFIG)."""

import uvicorn

from shared.auth import Authenticator, AuthSettings
from shared.config import load_section
from shared.logs import configure_logging

from .app import create_app
from .settings import GatewaySettings

configure_logging()
settings = load_section(GatewaySettings, "mcp_gateway")
authenticator = Authenticator(load_section(AuthSettings, "auth"))
uvicorn.run(
    create_app(settings, authenticator),
    host=settings.host,
    port=settings.port,
    access_log=False,  # the gateway logs each request itself, with the caller and decision
    log_config=None,
)
