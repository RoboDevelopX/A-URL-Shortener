"""FastAPI dependencies: the only place that knows how request-scoped objects are built.

Routers declare what they need (a session, a service) and FastAPI wires it up per request.
Tests override these functions instead of monkeypatching modules.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.redis import RedisGateway


def get_settings_dep(request: Request) -> Settings:
    """Settings the app was created with (lets tests run apps with different settings)."""
    settings: Settings = request.app.state.settings
    return settings


def get_redis(request: Request) -> RedisGateway:
    """The process-wide Redis gateway created in the lifespan."""
    redis: RedisGateway = request.app.state.redis
    return redis


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    """One session per request; it is closed (and its connection returned) afterwards."""
    factory: async_sessionmaker[AsyncSession] = request.app.state.session_factory
    async with factory() as session:
        yield session


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
RedisDep = Annotated[RedisGateway, Depends(get_redis)]
SessionDep = Annotated[AsyncSession, Depends(get_session)]
