"""Request and response bodies for the links API."""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

EffectiveStatus = Literal["active", "disabled", "expired"]


class LinkCreate(BaseModel):
    """Body of ``POST /api/v1/links``."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        min_length=1, max_length=4096, examples=["https://example.com/a/very/long/path"]
    )
    alias: str | None = Field(default=None, examples=["spring-sale"])
    expires_at: AwareDatetime | None = Field(
        default=None,
        description="ISO 8601 timestamp with a timezone; omit for a link that never expires.",
    )


class LinkUpdate(BaseModel):
    """Body of ``PATCH /api/v1/links/{code}``. ``null`` removes the expiry."""

    model_config = ConfigDict(extra="forbid")

    expires_at: AwareDatetime | None


class LinkResponse(BaseModel):
    """A link as returned by the management API."""

    code: str
    short_url: str
    long_url: str
    is_custom: bool
    status: EffectiveStatus
    expires_at: datetime | None
    created_at: datetime
