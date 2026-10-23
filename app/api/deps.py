"""FastAPI dependencies: the only place that knows how request-scoped objects are built.

Routers declare what they need (a session, a service) and FastAPI wires it up per request.
Tests override these functions instead of monkeypatching modules.
"""

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.errors import AppError
from app.core.redis import RedisGateway
from app.models.api_key import ApiKey
from app.services.analytics import ClickRecorder
from app.services.auth import ApiKeyService
from app.services.cache import LinkCache
from app.services.codes import ShortCodeGenerator
from app.services.links import LinkService
from app.services.rate_limit import RateLimitRule, SlidingWindowRateLimiter
from app.services.redirects import RedirectService
from app.services.screening import UrlScreener
from app.services.stats import StatsService


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


def get_code_generator(request: Request) -> ShortCodeGenerator:
    codes: ShortCodeGenerator = request.app.state.codes
    return codes


def get_link_cache(redis: RedisDep, settings: SettingsDep) -> LinkCache:
    return LinkCache(redis, settings.cache_ttl_seconds, settings.negative_cache_ttl_seconds)


LinkCacheDep = Annotated[LinkCache, Depends(get_link_cache)]


def get_screener(request: Request) -> UrlScreener:
    screener: UrlScreener = request.app.state.screener
    return screener


def get_link_service(
    session: SessionDep,
    settings: SettingsDep,
    codes: Annotated[ShortCodeGenerator, Depends(get_code_generator)],
    cache: LinkCacheDep,
    screener: Annotated[UrlScreener, Depends(get_screener)],
) -> LinkService:
    return LinkService(session, codes=codes, cache=cache, screener=screener, settings=settings)


def get_redirect_service(session: SessionDep, cache: LinkCacheDep) -> RedirectService:
    return RedirectService(session, cache)


LinkServiceDep = Annotated[LinkService, Depends(get_link_service)]
RedirectServiceDep = Annotated[RedirectService, Depends(get_redirect_service)]


def get_click_recorder(request: Request, redis: RedisDep, settings: SettingsDep) -> ClickRecorder:
    return ClickRecorder(redis, request.app.state.session_factory, settings)


def get_stats_service(session: SessionDep) -> StatsService:
    return StatsService(session)


ClickRecorderDep = Annotated[ClickRecorder, Depends(get_click_recorder)]
StatsServiceDep = Annotated[StatsService, Depends(get_stats_service)]


# Declared as a security scheme so the OpenAPI docs show an "Authorize" button.
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def get_current_api_key(
    session: SessionDep,
    settings: SettingsDep,
    raw_key: Annotated[str | None, Security(api_key_header)],
) -> ApiKey:
    service = ApiKeyService(session, settings.api_key_pepper.get_secret_value().encode())
    return await service.authenticate(raw_key)


CurrentApiKey = Annotated[ApiKey, Depends(get_current_api_key)]


class RateLimitedError(AppError):
    status_code = 429
    code = "rate_limited"
    default_message = "Too many requests."


def get_create_limiter(request: Request) -> SlidingWindowRateLimiter:
    limiter: SlidingWindowRateLimiter = request.app.state.create_limiter
    return limiter


async def enforce_create_rate_limit(
    request: Request,
    api_key: CurrentApiKey,
    settings: SettingsDep,
    limiter: Annotated[SlidingWindowRateLimiter, Depends(get_create_limiter)],
) -> None:
    """Limit link creation per API key and per client IP; 429 with Retry-After if exceeded.

    The client IP is what uvicorn reports. Behind a load balancer, run uvicorn with
    ``--proxy-headers --forwarded-allow-ips=<lb address>`` so this is the real client and not
    the balancer (and never trust X-Forwarded-For from arbitrary clients).
    """
    client_ip = request.client.host if request.client else "unknown"
    decision = await limiter.hit(
        [
            RateLimitRule(f"key:{api_key.id}", settings.rate_limit_create_per_key),
            RateLimitRule(f"ip:{client_ip}", settings.rate_limit_create_per_ip),
        ]
    )
    if not decision.allowed:
        raise RateLimitedError(
            f"Rate limit exceeded; retry in {decision.retry_after_seconds}s.",
            headers={"Retry-After": str(decision.retry_after_seconds)},
        )
