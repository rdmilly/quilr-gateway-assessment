"""Optional JWT mode: only tokens signed for this gateway, by this issuer, get in."""

from __future__ import annotations

import time
from typing import Any

import jwt
import pytest
from pydantic import ValidationError

from shared.auth import (
    Authenticator,
    AuthSettings,
    Identity,
    JwtSettings,
    Role,
    TokenEntry,
    hash_token,
)

SECRET = "test-signing-secret-" + "0123456789abcdef" * 3  # 67 bytes, fake
ENV = {"GATEWAY_JWT_SECRET": SECRET}
ISSUER = "https://idp.example"
AUDIENCE = "https://gateway.example/mcp"


def settings(**jwt_overrides: Any) -> AuthSettings:
    return AuthSettings(jwt=JwtSettings(issuer=ISSUER, audience=AUDIENCE, **jwt_overrides))


def make_token(secret: str = SECRET, algorithm: str = "HS256", **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "carol",
        "role": "admin",
        "iss": ISSUER,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + 300,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, secret, algorithm=algorithm)


def identify(token: str, auth_settings: AuthSettings | None = None) -> Identity | None:
    return Authenticator(auth_settings or settings(), environ=ENV).identify(f"Bearer {token}")


@pytest.mark.parametrize("role", ["admin", "viewer"])
def test_valid_token_gives_subject_and_role(role: str) -> None:
    assert identify(make_token(role=role)) == Identity("carol", Role(role))


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "https://other-service.example"},  # issued for someone else
        {"iss": "https://evil-idp.example"},
        {"exp": int(time.time()) - 3600},  # expired
        {"nbf": int(time.time()) + 3600},  # not valid yet
        {"role": "superuser"},
        {"role": None},
        {"sub": None},
        {"exp": None},
        {"aud": None},
        {"iat": None},
    ],
)
def test_tokens_that_fail_a_claim_check_are_refused(overrides: dict[str, Any]) -> None:
    assert identify(make_token(**overrides)) is None


def test_wrong_signature_is_refused() -> None:
    assert identify(make_token(secret="another-fake-secret-" + "fedcba9876543210" * 3)) is None


def test_algorithm_is_pinned() -> None:
    assert identify(make_token(algorithm="HS512")) is None
    unsigned = jwt.encode(
        {
            "sub": "carol",
            "role": "admin",
            "iss": ISSUER,
            "aud": AUDIENCE,
            "iat": int(time.time()),
            "exp": int(time.time()) + 300,
        },
        "",
        algorithm="none",
    )
    assert identify(unsigned) is None


def test_garbage_is_refused() -> None:
    assert identify("not.a.jwt") is None
    assert identify("only-one-part") is None


def test_static_tokens_still_work_alongside_jwt() -> None:
    both = AuthSettings(
        tokens=(TokenEntry(sha256=hash_token("static-token"), subject="sam", role=Role.VIEWER),),
        jwt=JwtSettings(issuer=ISSUER, audience=AUDIENCE),
    )
    auth = Authenticator(both, environ=ENV)
    assert auth.identify("Bearer static-token") == Identity("sam", Role.VIEWER)
    assert auth.identify(f"Bearer {make_token()}") == Identity("carol", Role.ADMIN)


@pytest.mark.parametrize("environ", [{}, {"GATEWAY_JWT_SECRET": "too-short"}])
def test_missing_or_short_secret_stops_startup_without_showing_it(
    environ: dict[str, str],
) -> None:
    with pytest.raises(ValueError, match="GATEWAY_JWT_SECRET must be set") as exc:
        Authenticator(settings(), environ=environ)
    assert "too-short" not in str(exc.value)


def test_secret_variable_name_is_configurable() -> None:
    custom = settings(secret_env="MY_JWT_KEY")
    auth = Authenticator(custom, environ={"MY_JWT_KEY": SECRET})
    assert auth.identify(f"Bearer {make_token()}") is not None


def test_config_needs_tokens_or_jwt() -> None:
    with pytest.raises(ValidationError, match="at least one token"):
        AuthSettings()


def test_jwt_section_rejects_other_algorithms() -> None:
    with pytest.raises(ValidationError):
        JwtSettings(issuer=ISSUER, audience=AUDIENCE, algorithm="none")  # type: ignore[arg-type]
