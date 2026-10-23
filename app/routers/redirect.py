"""Public redirect endpoint."""

from fastapi import APIRouter, status
from fastapi.responses import RedirectResponse

from app.api.deps import RedirectServiceDep
from app.schemas.errors import ErrorResponse

router = APIRouter(tags=["redirect"])


@router.get(
    "/{code}",
    status_code=status.HTTP_302_FOUND,
    response_class=RedirectResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Unknown code"},
        410: {"model": ErrorResponse, "description": "Expired or disabled"},
    },
)
async def follow_link(code: str, service: RedirectServiceDep) -> RedirectResponse:
    """302 to the destination.

    302 rather than 301: browsers cache 301s permanently, so later clicks would never reach
    us (no analytics) and disabling or re-targeting a link would not take effect for anyone
    who had already clicked it. ``no-store`` keeps intermediaries from caching it too.
    """
    target = await service.resolve(code)
    return RedirectResponse(
        target.long_url,
        status_code=status.HTTP_302_FOUND,
        headers={"Cache-Control": "no-store"},
    )
