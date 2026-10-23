"""Shared test configuration.

Integration tests run against real Postgres and Redis (``make test`` starts them with docker
compose). The URLs can be overridden with TEST_DATABASE_URL / TEST_REDIS_URL, which CI does.
"""

import os
from pathlib import Path

from app.core.config import Settings

MODEL_PATH = Path(__file__).resolve().parents[1] / "artifacts" / "url_classifier.joblib"

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://shortener:shortener-dev-password@localhost:5432/shortener_test",
)
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


def make_settings(**overrides: object) -> Settings:
    """Settings for tests; keyword arguments override individual fields."""
    values: dict[str, object] = {
        "log_level": "WARNING",
        "database_url": TEST_DATABASE_URL,
        "redis_url": TEST_REDIS_URL,
        "code_secret": "test-code-secret",
        "api_key_pepper": "test-pepper",
        "model_path": MODEL_PATH,
    }
    values.update(overrides)
    return Settings.model_validate(values)
