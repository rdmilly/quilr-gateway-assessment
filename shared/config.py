"""Settings, read from one TOML file and checked once at startup.

The file is config.toml in the working directory, or the path in GATEWAY_CONFIG. Each part of
the system reads its own section into its own model. If the file is missing or a value is
wrong, startup stops with a message naming the problem, rather than running with a weak or
partial configuration.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

CONFIG_ENV_VAR: Final = "GATEWAY_CONFIG"
DEFAULT_CONFIG_PATH: Final = Path("config.toml")


class ConfigError(Exception):
    """The configuration is missing or invalid, so startup must stop."""


class Section(BaseModel):
    """Base for every settings section: unknown keys are errors, values can't change later."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def config_path() -> Path:
    return Path(os.environ.get(CONFIG_ENV_VAR, str(DEFAULT_CONFIG_PATH)))


def load_section[T: Section](model: type[T], section: str, path: Path | None = None) -> T:
    """Read one [section] of the config file into `model`, or raise ConfigError."""
    path = path or config_path()
    try:
        with path.open("rb") as file:
            data = tomllib.load(file)
    except FileNotFoundError:
        raise ConfigError(f"Config file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Config file is not valid TOML: {path}: {exc}") from None

    raw = data.get(section)
    if not isinstance(raw, dict):
        raise ConfigError(f"Config file {path} has no [{section}] section")
    try:
        return model.model_validate(raw)
    except ValidationError as exc:
        # Name each bad field and the rule it broke, but never echo the value: it could be a
        # secret, and this message ends up in logs.
        problems = "; ".join(
            f"{section}.{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
        raise ConfigError(f"Invalid settings in {path}: {problems}") from None
