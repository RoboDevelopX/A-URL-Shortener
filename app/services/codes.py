"""Short-code generation that cannot collide, plus custom-alias rules.

How "cannot collide" is guaranteed
----------------------------------
Every link gets a unique integer id from a Postgres sequence. The code is
``base62(permute(id))`` where ``permute`` is a *bijection* on ``[0, 62**length)``: a keyed
Feistel network. A bijection maps distinct inputs to distinct outputs, so distinct ids
always give distinct codes. No "generate, check, retry" loop is needed, and there is no race
between two API instances because the sequence hands out each id exactly once.

Why not just ``base62(id)``? Sequential codes (``0000001``, ``0000002``...) leak how many
links exist and let anyone enumerate every link. The permutation makes codes look random
while keeping the uniqueness proof trivial.

Feistel networks are invertible whatever the round function is, which is what makes them a
permutation. They work on bit strings of even length, so we permute ``[0, 2**bits)`` and use
"cycle walking": if the output falls outside ``[0, 62**length)``, permute again. Since we walk
along a cycle of a permutation, we always come back into the domain, and the result is still
a bijection on the domain (on average 1.25 steps for length 7).
"""

import hashlib
import hmac
import re
import string

from app.core.errors import AppError

BASE62_ALPHABET = string.digits + string.ascii_uppercase + string.ascii_lowercase
_BASE62_INDEX = {char: index for index, char in enumerate(BASE62_ALPHABET)}

ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_-]{3,32}$")
# Compared case-insensitively: ``/Docs`` would not clash in routing, but it would confuse.
RESERVED_ALIASES = frozenset(
    {"api", "admin", "docs", "redoc", "openapi", "healthz", "readyz", "static", "metrics"}
)


class InvalidAliasError(AppError):
    status_code = 422
    code = "invalid_alias"
    default_message = "Alias is not allowed."


def base62_encode(value: int, width: int) -> str:
    """Encode a non-negative integer as base62, left-padded with ``0`` to ``width`` chars."""
    if value < 0:
        raise ValueError("value must be non-negative")
    chars: list[str] = []
    while value:
        value, remainder = divmod(value, 62)
        chars.append(BASE62_ALPHABET[remainder])
    encoded = "".join(reversed(chars)) or "0"
    if len(encoded) > width:
        raise ValueError(f"value does not fit in {width} base62 characters")
    return encoded.rjust(width, "0")


def base62_decode(code: str) -> int:
    """Inverse of :func:`base62_encode`."""
    value = 0
    for char in code:
        try:
            value = value * 62 + _BASE62_INDEX[char]
        except KeyError:
            raise ValueError(f"invalid base62 character: {char!r}") from None
    return value


class ShortCodeGenerator:
    """Map sequence ids to fixed-length, non-sequential base62 codes, one-to-one."""

    ROUNDS = 4  # four Feistel rounds give a strong pseudo-random permutation (Luby-Rackoff)

    def __init__(self, secret: bytes, length: int) -> None:
        if length < 1:
            raise ValueError("length must be positive")
        self.length = length
        self.capacity: int = 62**length
        bits = (self.capacity - 1).bit_length()
        self._half_bits: int = (bits + 1) // 2
        self._half_mask: int = (1 << self._half_bits) - 1
        self._secret = secret

    def _round(self, index: int, half: int) -> int:
        digest = hmac.new(
            self._secret, bytes([index]) + half.to_bytes(8, "big"), hashlib.sha256
        ).digest()
        return int.from_bytes(digest[:8], "big") & self._half_mask

    def _permute(self, value: int) -> int:
        left, right = value >> self._half_bits, value & self._half_mask
        for index in range(self.ROUNDS):
            left, right = right, left ^ self._round(index, right)
        return (left << self._half_bits) | right

    def _unpermute(self, value: int) -> int:
        left, right = value >> self._half_bits, value & self._half_mask
        for index in reversed(range(self.ROUNDS)):
            left, right = right ^ self._round(index, left), left
        return (left << self._half_bits) | right

    def encode(self, link_id: int) -> str:
        """Return the short code for ``link_id``; distinct ids always give distinct codes."""
        if not 0 <= link_id < self.capacity:
            raise ValueError(f"id {link_id} is outside the code space of {self.capacity}")
        value = self._permute(link_id)
        while value >= self.capacity:
            value = self._permute(value)
        return base62_encode(value, self.length)

    def decode(self, code: str) -> int:
        """Recover the id from a generated code (the proof that ``encode`` is invertible)."""
        value = base62_decode(code)
        if len(code) != self.length or value >= self.capacity:
            raise ValueError("not a code produced by this generator")
        value = self._unpermute(value)
        while value >= self.capacity:
            value = self._unpermute(value)
        return value


def validate_alias(alias: str) -> str:
    """Return ``alias`` if it is a legal custom alias, else raise ``InvalidAliasError``."""
    if not ALIAS_PATTERN.fullmatch(alias):
        raise InvalidAliasError("Alias must be 3-32 characters of letters, digits, '-' or '_'.")
    if alias.lower() in RESERVED_ALIASES:
        raise InvalidAliasError(f"Alias '{alias}' is reserved.")
    return alias
