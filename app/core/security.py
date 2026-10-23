"""API key generation and hashing.

Keys are 32 random bytes, so they cannot be guessed or brute-forced. That is why a fast
keyed hash (HMAC-SHA256) is the right tool here, while passwords need slow hashes (bcrypt,
argon2): slow hashing exists to protect *low-entropy* secrets, and on a high-entropy key it
would only add ~100 ms to every API call. The server-side pepper means a leaked database
dump alone is not enough to check candidate keys offline.
"""

import hashlib
import hmac
import secrets

KEY_PREFIX = "usk_"
DISPLAY_PREFIX_LENGTH = 12


def generate_api_key() -> tuple[str, str]:
    """Return ``(key, display_prefix)``. The key is shown once and never stored."""
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    return key, key[:DISPLAY_PREFIX_LENGTH]


def hash_api_key(key: str, pepper: bytes) -> str:
    return hmac.new(pepper, key.encode(), hashlib.sha256).hexdigest()
