"""Test doubles."""

from app.services.screening import UrlScreener

STUB_BLOCK = 0.9
STUB_REVIEW = 0.5


class FixedScorer:
    """A ``UrlScorer`` that returns a fixed score, or a per-URL score when one is set."""

    version = "stub-model-1"

    def __init__(self, default: float = 0.01, overrides: dict[str, float] | None = None) -> None:
        self.default = default
        self.overrides = overrides or {}

    def score(self, url: str) -> float:
        return self.overrides.get(url, self.default)


def stub_screener(default: float = 0.01, overrides: dict[str, float] | None = None) -> UrlScreener:
    return UrlScreener(FixedScorer(default, overrides), STUB_BLOCK, STUB_REVIEW)


def allow_all_screener() -> UrlScreener:
    """Zero-argument factory for ``dependency_overrides`` (FastAPI inspects the signature)."""
    return stub_screener()
