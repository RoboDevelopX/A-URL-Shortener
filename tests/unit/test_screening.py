import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.ml.classifier import ModelLoadError, UrlClassifier
from app.models.screening import ScreeningDecision
from app.services.screening import UrlScreener
from tests.conftest import make_settings
from tests.stubs import FixedScorer


@pytest.mark.parametrize(
    ("score", "decision"),
    [
        (0.0, ScreeningDecision.ALLOW),
        (0.4999, ScreeningDecision.ALLOW),
        (0.5, ScreeningDecision.REVIEW),
        (0.8999, ScreeningDecision.REVIEW),
        (0.9, ScreeningDecision.BLOCK),
        (1.0, ScreeningDecision.BLOCK),
    ],
)
def test_decision_bands(score: float, decision: ScreeningDecision) -> None:
    screener = UrlScreener(FixedScorer(), block_threshold=0.9, review_threshold=0.5)
    assert screener.decide(score) is decision


def test_equal_thresholds_disable_the_review_band() -> None:
    screener = UrlScreener(FixedScorer(), block_threshold=0.8, review_threshold=0.8)
    assert screener.decide(0.79) is ScreeningDecision.ALLOW
    assert screener.decide(0.8) is ScreeningDecision.BLOCK


@pytest.mark.parametrize(("block", "review"), [(0.5, 0.9), (1.1, 0.5), (0.5, -0.1)])
def test_invalid_thresholds_are_rejected(block: float, review: float) -> None:
    with pytest.raises(ValueError, match="thresholds"):
        UrlScreener(FixedScorer(), block_threshold=block, review_threshold=review)


def test_settings_reject_review_above_block() -> None:
    with pytest.raises(ValidationError, match="must not exceed"):
        make_settings(ml_block_threshold=0.5, ml_review_threshold=0.6)


async def test_screen_returns_score_decision_and_version() -> None:
    screener = UrlScreener(FixedScorer(default=0.95), block_threshold=0.9, review_threshold=0.5)
    result = await screener.screen("https://example.com/")
    assert result.decision is ScreeningDecision.BLOCK
    assert result.score == 0.95
    assert result.model_version == screener.model_version == "stub-model-1"


def test_classifier_rejects_unreadable_metadata(tmp_path: Path) -> None:
    artifact = tmp_path / "model.joblib"
    artifact.write_bytes(b"x")
    artifact.with_suffix(".json").write_text("{not json")
    with pytest.raises(ModelLoadError, match="metadata"):
        UrlClassifier.load(artifact)


def test_classifier_reports_a_missing_artifact(tmp_path: Path) -> None:
    artifact = tmp_path / "model.joblib"
    artifact.with_suffix(".json").write_text(
        json.dumps(
            {
                "version": "v",
                "artifact_sha256": "0" * 64,
                "thresholds": {"block": 0.9, "review": 0.5},
            }
        )
    )
    with pytest.raises(ModelLoadError, match="cannot read model artifact"):
        UrlClassifier.load(artifact)
