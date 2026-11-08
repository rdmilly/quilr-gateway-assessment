"""Bearer token to identity, for the gateway's front door (Tasks 2 to 4).

Tokens are never stored. The config holds the SHA-256 hash of each token, and a presented token
is hashed and compared against every entry in constant time, so neither the answer nor its
timing reveals anything about which tokens exist.

Plain SHA-256 is right here because these are long random API tokens, not passwords. A password
would need a slow, salted hash such as Argon2.

Optionally, the gateway also accepts signed JWTs (``[auth.jwt]`` in the config). A JWT is accepted
only if its signature is valid, its algorithm is the one configured (``alg: none`` and algorithm
swaps are refused), it is unexpired, and its issuer and audience are exactly the configured ones.
The audience check is the important part: a token issued for some other service is refused, even
if the same identity provider signed it. That is MCP's "no token passthrough" rule seen from the
receiving side. The signing secret comes from an environment variable, never from the config file.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Literal

import jwt
from pydantic import Field, field_validator, model_validator

from shared.config import Section

MAX_HEADER_LENGTH: Final = 4096

# Exactly "Bearer <token>": the scheme is case-insensitive (RFC 7235), one space, then RFC 6750
# token characters. Anything else is refused rather than guessed at.
_BEARER: Final = re.compile(r"[Bb][Ee][Aa][Rr][Ee][Rr] ([A-Za-z0-9\-._~+/]+=*)")


class Role(StrEnum):
    ADMIN = "admin"
    VIEWER = "viewer"


@dataclass(frozen=True)
class Identity:
    subject: str
    role: Role


class TokenEntry(Section):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject: str = Field(min_length=1, max_length=128)
    role: Role


MIN_JWT_SECRET_BYTES: Final = 32  # RFC 7518: an HS256 key should be at least as long as the hash


class JwtSettings(Section):
    issuer: str = Field(min_length=1)
    audience: str = Field(min_length=1)  # this gateway's own identifier
    secret_env: str = Field(default="GATEWAY_JWT_SECRET", pattern=r"^[A-Z][A-Z0-9_]*$")
    algorithm: Literal["HS256"] = "HS256"
    role_claim: str = Field(default="role", min_length=1)
    leeway_s: int = Field(default=30, ge=0, le=300)


class AuthSettings(Section):
    tokens: tuple[TokenEntry, ...] = ()
    jwt: JwtSettings | None = None

    @model_validator(mode="after")
    def _some_way_in(self) -> AuthSettings:
        if not self.tokens and self.jwt is None:
            raise ValueError("configure at least one token, or [auth.jwt]")
        return self

    @field_validator("tokens")
    @classmethod
    def _no_duplicate_tokens(cls, tokens: tuple[TokenEntry, ...]) -> tuple[TokenEntry, ...]:
        hashes = [entry.sha256 for entry in tokens]
        if len(set(hashes)) != len(hashes):
            raise ValueError("the same token hash is listed more than once")
        return tokens


def hash_token(token: str) -> str:
    """The value to put in config for a token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_bearer(header: str | None) -> str | None:
    """Return the token from an Authorization header, or None unless it is exactly Bearer form."""
    if header is None or len(header) > MAX_HEADER_LENGTH:
        return None
    match = _BEARER.fullmatch(header)
    return match.group(1) if match else None


class Authenticator:
    """Answers who a request is from, or None if it isn't from anyone we know."""

    def __init__(self, settings: AuthSettings, environ: Mapping[str, str] | None = None) -> None:
        self._entries = tuple(
            (bytes.fromhex(entry.sha256), Identity(entry.subject, entry.role))
            for entry in settings.tokens
        )
        self._jwt = settings.jwt
        self._jwt_secret = b""
        if self._jwt is not None:
            env = os.environ if environ is None else environ
            secret = env.get(self._jwt.secret_env, "")
            if len(secret.encode("utf-8")) < MIN_JWT_SECRET_BYTES:
                # Name the variable, never the value.
                raise ValueError(
                    f"{self._jwt.secret_env} must be set to a secret of at least "
                    f"{MIN_JWT_SECRET_BYTES} bytes"
                )
            self._jwt_secret = secret.encode("utf-8")

    @property
    def entry_count(self) -> int:
        return len(self._entries)

    def identify(self, authorization: str | None) -> Identity | None:
        token = parse_bearer(authorization)
        if token is None:
            return None
        presented = hashlib.sha256(token.encode("utf-8")).digest()
        found: Identity | None = None
        for stored, identity in self._entries:
            # Compare against every entry, even after a match, so the time taken doesn't
            # depend on where (or whether) the token appears in the list.
            if hmac.compare_digest(presented, stored):
                found = identity
        if found is None and self._jwt is not None and token.count(".") == 2:
            found = self._identify_jwt(token, self._jwt)
        return found

    def _identify_jwt(self, token: str, settings: JwtSettings) -> Identity | None:
        try:
            claims = jwt.decode(
                token,
                self._jwt_secret,
                algorithms=[settings.algorithm],  # pinned: no "none", no algorithm swap
                audience=settings.audience,
                issuer=settings.issuer,
                leeway=settings.leeway_s,
                options={"require": ["exp", "iat", "sub", "aud", "iss"]},
            )
        except jwt.InvalidTokenError:
            return None
        subject = claims.get("sub")
        role = claims.get(settings.role_claim)
        if not isinstance(subject, str) or not subject or role not in {r.value for r in Role}:
            return None
        return Identity(subject[:128], Role(role))
