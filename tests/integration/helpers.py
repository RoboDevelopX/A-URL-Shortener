"""Helpers shared by integration tests."""

from fastapi import FastAPI

from app.services.auth import ApiKeyService


async def issue_key(app: FastAPI, name: str = "test") -> str:
    """Create an API key through the real service and return its plaintext."""
    pepper = app.state.settings.api_key_pepper.get_secret_value().encode()
    async with app.state.session_factory() as session:
        raw_key, _ = await ApiKeyService(session, pepper).issue(name)
    return raw_key
