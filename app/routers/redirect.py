"""Public redirect endpoint."""

from fastapi import APIRouter, BackgroundTasks, Request, status
from fastapi.responses import RedirectResponse

from app.api.deps import ClickRecorderDep, RedirectServiceDep
from app.schemas.errors import ErrorResponse
from app.services.analytics import ClickEvent
from app.services.links import utcnow

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
async def follow_link(
    code: str,
    request: Request,
    background: BackgroundTasks,
    service: RedirectServiceDep,
    recorder: ClickRecorderDep,
) -> RedirectResponse:
    """302 to the destination.

    302 rather than 301: browsers cache 301s permanently, so later clicks would never reach
    us (no analytics) and disabling or re-targeting a link would not take effect for anyone
    who had already clicked it. ``no-store`` keeps intermediaries from caching it too.

    The click is recorded in a background task, which Starlette runs after the response
    has been sent, so analytics add no latency to the redirect.
    """
    target = await service.resolve(code)
    event = ClickEvent(
        link_id=target.link_id,
        clicked_at=utcnow(),
        referrer=request.headers.get("referer"),
        user_agent=request.headers.get("user-agent"),
    )
    background.add_task(recorder.record, event)
    return RedirectResponse(
        target.long_url,
        status_code=status.HTTP_302_FOUND,
        headers={"Cache-Control": "no-store"},
    )
