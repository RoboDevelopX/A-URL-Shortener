"""Application settings.

Every tunable comes from an environment variable (or a local ``.env`` file in development).
Secrets have no defaults, so a missing secret fails loudly at startup instead of silently
running with a value that was committed to git.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed view over the process environment."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: Literal["dev", "test", "prod"] = "dev"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    database_url: str = Field(
        description="SQLAlchemy async URL, e.g. postgresql+asyncpg://user:pass@host:5432/db",
    )
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=10, ge=0)

    redis_url: str = Field(description="redis://host:6379/0")
    # Short timeouts on purpose: Redis is an optimisation on the hot path, so when it is
    # slow we would rather fall back to Postgres quickly than make every redirect wait.
    redis_socket_timeout_seconds: float = Field(default=0.25, gt=0)
    redis_circuit_cooldown_seconds: float = Field(default=5.0, gt=0)

    # Bounded on purpose: if an invalidation is lost (Redis down during a PATCH), a stale
    # entry can live at most this long.
    cache_ttl_seconds: int = Field(default=600, ge=1)
    # Caching "this code does not exist" shields Postgres from bots scanning random codes.
    negative_cache_ttl_seconds: int = Field(default=30, ge=1)

    api_key_pepper: SecretStr = Field(
        description="Server-side secret mixed into API key hashes (HMAC key).",
    )
    rate_limit_window_seconds: int = Field(default=60, ge=1)
    rate_limit_create_per_key: int = Field(default=60, ge=1)
    rate_limit_create_per_ip: int = Field(default=120, ge=1)

    click_stream_key: str = "clicks:stream"
    click_consumer_group: str = "click-writers"
    # Safety cap on the stream if the worker falls far behind. Processed entries are deleted
    # as soon as they are written, so in normal operation the stream stays near-empty.
    click_stream_max_length: int = Field(default=500_000, ge=1000)
    worker_batch_size: int = Field(default=500, ge=1, le=10_000)
    worker_block_ms: int = Field(default=1000, ge=10)
    # A message delivered to a worker that died is re-claimed by another after this long.
    worker_claim_idle_ms: int = Field(default=60_000, ge=1000)

    public_base_url: HttpUrl = Field(
        default=HttpUrl("http://localhost:8000"),
        description="Origin that short links are served from; prefixed to every code.",
    )
    code_length: int = Field(default=7, ge=5, le=12)
    code_secret: SecretStr = Field(
        description=(
            "Key for the short-code permutation. Changing it re-maps future ids, so set it "
            "once per deployment and never rotate it."
        ),
    )
    max_url_length: int = Field(default=2048, ge=64, le=2048)

    @property
    def short_link_origin(self) -> str:
        """``public_base_url`` without a trailing slash."""
        return str(self.public_base_url).rstrip("/")


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read from the environment once."""
    return Settings()
