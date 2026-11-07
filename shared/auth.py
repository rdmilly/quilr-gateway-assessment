"""Bearer token to identity, for the gateway's front door (Tasks 2 to 4).

Tokens are never stored. The config holds the SHA-256 hash of each token, and a presented token
is hashed and compared against every entry in constant time, so neither the answer nor its
timing reveals anything about which tokens exist.

Plain SHA-256 is right here because these are long random API tokens, not passwords. A password
would need a slow, salted hash such as Argon2.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from pydantic import Field, field_validator

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


class AuthSettings(Section):
    tokens: tuple[TokenEntry, ...] = Field(min_length=1)

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

    def __init__(self, settings: AuthSettings) -> None:
        self._entries = tuple(
            (bytes.fromhex(entry.sha256), Identity(entry.subject, entry.role))
            for entry in settings.tokens
        )

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
        return found
