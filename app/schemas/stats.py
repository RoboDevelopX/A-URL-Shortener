"""Response body for ``GET /api/v1/links/{code}/stats``."""

from datetime import date

from pydantic import BaseModel, Field


class DailyClicks(BaseModel):
    date: date
    clicks: int


class ReferrerCount(BaseModel):
    referrer: str = Field(description='Referrer host, or "(direct)" when none was sent.')
    clicks: int


class BrowserCount(BaseModel):
    browser: str
    clicks: int


class LinkStats(BaseModel):
    code: str
    window_days: int = Field(description="Size of the window used by every field but total.")
    total_clicks: int = Field(description="All-time clicks.")
    daily: list[DailyClicks] = Field(description="One entry per UTC day, oldest first.")
    top_referrers: list[ReferrerCount]
    devices: dict[str, int] = Field(examples=[{"desktop": 10, "mobile": 4, "tablet": 0}])
    browsers: list[BrowserCount]
