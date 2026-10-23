from datetime import UTC, datetime

import pytest

from app.models.click import DeviceType
from app.services.analytics import (
    MAX_USER_AGENT_LENGTH,
    ClickEvent,
    build_click_row,
    classify_user_agent,
    referrer_host,
)

IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
IPAD = (
    "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)
CHROME_WINDOWS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"


@pytest.mark.parametrize(
    ("user_agent", "device", "browser", "os_family"),
    [
        (IPHONE, DeviceType.MOBILE, "Mobile Safari", "iOS"),
        (IPAD, DeviceType.TABLET, "Mobile Safari", "iOS"),
        (CHROME_WINDOWS, DeviceType.DESKTOP, "Chrome", "Windows"),
        (GOOGLEBOT, DeviceType.BOT, "Googlebot", "Other"),
        (None, DeviceType.OTHER, "Unknown", "Unknown"),
        ("curl/8.5.0", DeviceType.OTHER, "curl", "Other"),
    ],
)
def test_classify_user_agent(
    user_agent: str | None, device: DeviceType, browser: str, os_family: str
) -> None:
    assert classify_user_agent(user_agent) == (browser, os_family, device)


@pytest.mark.parametrize(
    ("referrer", "expected"),
    [
        ("https://News.Ycombinator.com/item?id=1", "news.ycombinator.com"),
        ("android-app://com.slack/", "com.slack"),
        (None, None),
        ("", None),
        ("not a url", None),
        ("http://[broken/", None),
    ],
)
def test_referrer_host(referrer: str | None, expected: str | None) -> None:
    assert referrer_host(referrer) == expected


def test_click_event_round_trips_through_stream_fields() -> None:
    event = ClickEvent(1, datetime(2026, 5, 1, 12, tzinfo=UTC), "https://a.com/", "ua")
    assert ClickEvent.from_fields(event.to_fields()) == event


def test_click_event_truncates_and_handles_missing_headers() -> None:
    event = ClickEvent(1, datetime(2026, 5, 1, tzinfo=UTC), None, "x" * 5000)
    fields = event.to_fields()
    assert fields["ref"] == ""
    assert len(fields["ua"]) == MAX_USER_AGENT_LENGTH
    assert ClickEvent.from_fields(fields).referrer is None


def test_build_click_row() -> None:
    event = ClickEvent(7, datetime(2026, 5, 1, tzinfo=UTC), "https://t.co/x", IPHONE)
    row = build_click_row("1-0", event)
    assert row == {
        "event_id": "1-0",
        "link_id": 7,
        "clicked_at": event.clicked_at,
        "referrer_host": "t.co",
        "browser": "Mobile Safari",
        "os": "iOS",
        "device_type": DeviceType.MOBILE,
    }
