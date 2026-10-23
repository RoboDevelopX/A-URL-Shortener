"""Application settings.

Every tunable comes from an environment variable (or a local ``.env`` file in development).
Secrets have no defaults, so a missing secret fails loudly at startup instead of silently
running with a value that was committed to git.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
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


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings, read from the environment once."""
    return Settings()
