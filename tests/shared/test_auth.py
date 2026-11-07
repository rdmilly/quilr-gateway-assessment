"""Auth: bearer token to identity, nothing guessed, constant-time comparison."""

import hmac
from pathlib import Path

import pytest

from shared.auth import (
    MAX_HEADER_LENGTH,
    Authenticator,
    AuthSettings,
    Identity,
    Role,
    TokenEntry,
    hash_token,
    parse_bearer,
)
from shared.config import ConfigError, load_section

EXAMPLE_CONFIG = Path(__file__).parents[2] / "config.example.toml"


@pytest.fixture
def authenticator() -> Authenticator:
    return Authenticator(load_section(AuthSettings, "auth", EXAMPLE_CONFIG))


def test_the_example_config_maps_test_tokens_to_roles(authenticator: Authenticator) -> None:
    assert authenticator.identify("Bearer test-admin-token") == Identity("alice", Role.ADMIN)
    assert authenticator.identify("Bearer test-viewer-token") == Identity("victor", Role.VIEWER)


def test_the_bearer_scheme_name_is_case_insensitive(authenticator: Authenticator) -> None:
    assert authenticator.identify("bearer test-admin-token") == Identity("alice", Role.ADMIN)


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Bearer wrong-token",
        "Basic dGVzdA==",
        "Bearer  test-admin-token",
        " Bearer test-admin-token",
        "Bearer test-admin-token ",
        "Bearer test-admin-token, Bearer other",
        "Bearer test admin token",
        "Bearer test-admin-token\n",
        "Token test-admin-token",
    ],
)
def test_A2_1_A2_2_anything_but_one_valid_bearer_token_is_refused(
    authenticator: Authenticator, header: str | None
) -> None:
    assert authenticator.identify(header) is None


def test_overlong_headers_are_refused_before_hashing() -> None:
    assert parse_bearer("Bearer " + "a" * MAX_HEADER_LENGTH) is None


def test_A2_6_every_entry_is_compared_whether_or_not_it_matches(
    authenticator: Authenticator, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    real = hmac.compare_digest

    def counting(a: bytes, b: bytes) -> bool:
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr(hmac, "compare_digest", counting)
    authenticator.identify("Bearer test-admin-token")
    assert len(calls) == authenticator.entry_count
    calls.clear()
    authenticator.identify("Bearer nobody")
    assert len(calls) == authenticator.entry_count


def test_hash_token_matches_sha256() -> None:
    assert hash_token("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_duplicate_tokens_in_config_stop_startup(tmp_path: Path) -> None:
    digest = hash_token("same")
    path = tmp_path / "config.toml"
    path.write_text(
        "[auth]\ntokens = [\n"
        f'  {{ sha256 = "{digest}", subject = "a", role = "admin" }},\n'
        f'  {{ sha256 = "{digest}", subject = "b", role = "viewer" }},\n'
        "]\n"
    )
    with pytest.raises(ConfigError, match="more than once"):
        load_section(AuthSettings, "auth", path)


def test_an_unknown_role_stops_startup(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        f'[auth]\ntokens = [{{ sha256 = "{hash_token("x")}", subject = "a", role = "root" }}]\n'
    )
    with pytest.raises(ConfigError, match="auth.tokens.0.role"):
        load_section(AuthSettings, "auth", path)


def test_a_plaintext_token_in_config_is_rejected() -> None:
    with pytest.raises(ValueError, match="sha256"):
        TokenEntry.model_validate({"sha256": "test-admin-token", "subject": "a", "role": "admin"})
