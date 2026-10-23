"""Application factory and lifespan.

``create_app`` takes its settings as an argument so tests can build isolated apps. uvicorn
runs it with ``--factory``, so importing this module has no side effects.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import Settings, get_settings
from app.core.db import create_engine, create_session_factory
from app.core.errors import register_error_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.core.redis import create_redis
from app.routers import health, links, redirect
from app.services.codes import ShortCodeGenerator


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build a FastAPI app wired to the given (or environment) settings."""
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_engine(settings)
        redis = create_redis(settings)
        app.state.settings = settings
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        app.state.redis = redis
        app.state.codes = ShortCodeGenerator(
            settings.code_secret.get_secret_value().encode(), settings.code_length
        )
        try:
            yield
        finally:
            await redis.client.aclose()
            await engine.dispose()

    app = FastAPI(
        title="URL Shortener",
        version="0.1.0",
        description="Collision-free short links with caching, analytics and ML URL screening.",
        lifespan=lifespan,
    )
    app.add_middleware(RequestContextMiddleware)
    register_error_handlers(app)
    app.include_router(health.router)
    app.include_router(links.router)
    # Last: ``/{code}`` matches any single path segment.
    app.include_router(redirect.router)
    return app
