"""Config: one checked file, and startup stops on anything wrong (A0.14)."""

from pathlib import Path

import pytest

from shared.config import CONFIG_ENV_VAR, ConfigError, Section, load_section


class _Demo(Section):
    limit: int
    name: str = "default"


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_a_valid_section_loads(tmp_path: Path) -> None:
    settings = load_section(_Demo, "demo", _write(tmp_path, "[demo]\nlimit = 5\n"))
    assert settings.limit == 5
    assert settings.name == "default"


def test_the_path_can_come_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONFIG_ENV_VAR, str(_write(tmp_path, "[demo]\nlimit = 7\n")))
    assert load_section(_Demo, "demo").limit == 7


def test_A0_14_a_missing_file_stops_startup(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_section(_Demo, "demo", tmp_path / "nope.toml")


def test_A0_14_a_missing_section_stops_startup(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"no \[demo\] section"):
        load_section(_Demo, "demo", _write(tmp_path, "[other]\nlimit = 1\n"))


def test_A0_14_an_unknown_setting_stops_startup(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="demo.limt"):
        load_section(_Demo, "demo", _write(tmp_path, "[demo]\nlimit = 1\nlimt = 2\n"))


def test_A0_14_broken_toml_stops_startup(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not valid TOML"):
        load_section(_Demo, "demo", _write(tmp_path, "[demo\nlimit = 1\n"))


def test_a_bad_value_is_named_but_never_echoed(tmp_path: Path) -> None:
    path = _write(tmp_path, '[demo]\nlimit = "sk-live-not-a-number"\n')
    with pytest.raises(ConfigError) as caught:
        load_section(_Demo, "demo", path)
    assert "demo.limit" in str(caught.value)
    assert "sk-live-not-a-number" not in str(caught.value)


def test_settings_cannot_be_changed_after_loading(tmp_path: Path) -> None:
    settings = load_section(_Demo, "demo", _write(tmp_path, "[demo]\nlimit = 5\n"))
    with pytest.raises(ValueError, match="frozen"):
        settings.limit = 6
