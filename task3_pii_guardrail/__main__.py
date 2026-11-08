"""``python -m task3_pii_guardrail`` runs the endpoint, reading config.toml (or $GATEWAY_CONFIG)."""

import uvicorn

from shared.config import load_section
from shared.logs import configure_logging

from .app import create_app
from .settings import LlmGatewaySettings

configure_logging()
settings = load_section(LlmGatewaySettings, "llm_gateway")
uvicorn.run(
    create_app(settings), host=settings.host, port=settings.port, access_log=False, log_config=None
)
