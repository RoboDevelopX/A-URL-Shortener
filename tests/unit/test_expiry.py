from datetime import UTC, datetime, timedelta

from app.models.link import LinkStatus
from app.services.links import effective_status

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_active_without_expiry() -> None:
    assert effective_status(LinkStatus.ACTIVE, None, NOW) == "active"


def test_active_before_expiry() -> None:
    assert effective_status(LinkStatus.ACTIVE, NOW + timedelta(seconds=1), NOW) == "active"


def test_expired_at_and_after_expiry() -> None:
    assert effective_status(LinkStatus.ACTIVE, NOW, NOW) == "expired"
    assert effective_status(LinkStatus.ACTIVE, NOW - timedelta(days=1), NOW) == "expired"


def test_disabled_wins_over_expiry() -> None:
    assert effective_status(LinkStatus.DISABLED, NOW - timedelta(days=1), NOW) == "disabled"
    assert effective_status(LinkStatus.DISABLED, None, NOW) == "disabled"
