"""Shared test configuration.

Integration tests run against real Postgres and Redis (``make test`` starts them with docker
compose). The URLs can be overridden with TEST_DATABASE_URL / TEST_REDIS_URL, which CI does.
"""

import os

from app.core.config import Settings

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://shortener:shortener-dev-password@localhost:5432/shortener_test",
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


def make_settings(**overrides: object) -> Settings:
    """Settings for tests; keyword arguments override individual fields."""
    values: dict[str, object] = {
        "environment": "test",
        "log_level": "WARNING",
        "database_url": TEST_DATABASE_URL,
        "redis_url": TEST_REDIS_URL,
        "code_secret": "test-code-secret",
    }
    values.update(overrides)
    return Settings.model_validate(values)
