"""Management API for links."""

from typing import Annotated

from fastapi import APIRouter, Query, Response, status

from app.api.deps import LinkServiceDep, SettingsDep, StatsServiceDep
from app.schemas.errors import ErrorResponse
from app.schemas.links import LinkCreate, LinkResponse, LinkUpdate
from app.schemas.stats import LinkStats
from app.services.links import to_response, utcnow

router = APIRouter(prefix="/api/v1/links", tags=["links"])

_ERRORS: dict[int | str, dict[str, object]] = {
    404: {"model": ErrorResponse, "description": "Unknown code"},
    422: {"model": ErrorResponse, "description": "Invalid input"},
}


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=LinkResponse,
    responses={
        409: {"model": ErrorResponse, "description": "Alias already taken"},
        422: {"model": ErrorResponse, "description": "Invalid URL, alias or expiry"},
    },
)
async def create_link(
    body: LinkCreate, service: LinkServiceDep, settings: SettingsDep, response: Response
) -> LinkResponse:
    """Create a short link, optionally with a custom alias and an expiry time."""
    link = await service.create(body)
    response.headers["Location"] = f"/api/v1/links/{link.code}"
    return to_response(link, settings.short_link_origin, utcnow())


@router.get("/{code}", response_model=LinkResponse, responses=_ERRORS)
async def get_link(code: str, service: LinkServiceDep, settings: SettingsDep) -> LinkResponse:
    """Return a link's metadata."""
    link = await service.get(code)
    return to_response(link, settings.short_link_origin, utcnow())


@router.patch(
    "/{code}",
    response_model=LinkResponse,
    responses={**_ERRORS, 410: {"model": ErrorResponse, "description": "Link is disabled"}},
)
async def update_link(
    code: str, body: LinkUpdate, service: LinkServiceDep, settings: SettingsDep
) -> LinkResponse:
    """Change or remove (``null``) a link's expiry."""
    link = await service.update_expiry(code, body.expires_at)
    return to_response(link, settings.short_link_origin, utcnow())


@router.delete("/{code}", status_code=status.HTTP_204_NO_CONTENT, responses=_ERRORS)
async def disable_link(code: str, service: LinkServiceDep) -> Response:
    """Disable a link. Redirects then return 410; the code is never reused."""
    await service.disable(code)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{code}/stats", response_model=LinkStats, responses=_ERRORS)
async def link_stats(
    code: str,
    service: LinkServiceDep,
    stats: StatsServiceDep,
    days: Annotated[int, Query(ge=1, le=365, description="Window size in UTC days")] = 30,
) -> LinkStats:
    """Click totals, daily clicks, top referrers, devices and browsers."""
    link = await service.get(code)
    return await stats.for_link(link, days)
