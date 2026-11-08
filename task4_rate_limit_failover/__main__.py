"""Run the Task 4 router: ``python -m task4_rate_limit_failover`` (reads config.toml)."""

import os
from pathlib import Path

import uvicorn

from shared.auth import Authenticator, AuthSettings
from shared.config import ConfigError, load_section
from shared.logs import configure_logging

from .app import create_app
from .limiter import SlidingWindowLimiter, StoreUnavailable
from .router import Provider
from .settings import RouterSettings

configure_logging()
settings = load_section(RouterSettings, "llm_router")
authenticator = Authenticator(load_section(AuthSettings, "auth"))


def key(env_name: str | None) -> str | None:
    return os.environ.get(env_name) if env_name else None


Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
try:
    limiter = SlidingWindowLimiter(settings.db_path, settings.tokens_per_window, settings.window_s)
except StoreUnavailable:
    raise ConfigError(f"Cannot open the rate limit database at {settings.db_path}") from None
providers = [
    Provider("primary", settings.primary_url, key(settings.primary_api_key_env)),
    Provider("backup", settings.backup_url, key(settings.backup_api_key_env)),
]
uvicorn.run(
    create_app(settings, authenticator, limiter, providers),
    host=settings.host,
    port=settings.port,
    access_log=False,
    log_config=None,
)
