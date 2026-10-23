"""The single error envelope every non-2xx response uses."""

from typing import Any

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    """Machine-readable code plus a human message; ``request_id`` ties it to the logs."""

    code: str = Field(examples=["alias_conflict"])
    message: str = Field(examples=["Alias 'promo' is already taken."])
    request_id: str | None = Field(default=None, examples=["5f0c6b1e9a4c4b0f8e2d7c3a1b9e4f6d"])
    details: list[dict[str, Any]] | None = None


class ErrorResponse(BaseModel):
    """``{"error": {...}}`` so clients can always look in the same place."""

    error: ErrorBody
