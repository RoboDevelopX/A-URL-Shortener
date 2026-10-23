"""ML screening on the create path: the real model for block/allow, stubs for the rest."""

import logging
import shutil
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from app.api.deps import get_screener
from app.main import build_screener, create_app
from app.ml.classifier import ModelLoadError
from app.models.link import Link
from app.models.screening import ScreeningDecision, ScreeningEvent
from app.services.screening import UrlScreener
from tests.conftest import MODEL_PATH, make_settings
from tests.stubs import FixedScorer, stub_screener

LINKS = "/api/v1/links"
PHISHING = "http://paypal.com.secure-login-verify-account.xyz/webscr/cmd=_login-submit/update.php"
BENIGN = "https://en.wikipedia.org/wiki/Python_(programming_language)"


@pytest.fixture
def real_model(app: FastAPI) -> FastAPI:
    app.dependency_overrides.pop(get_screener)
    return app


async def events(app: FastAPI) -> list[ScreeningEvent]:
    async with app.state.session_factory() as session:
        return list((await session.execute(select(ScreeningEvent))).scalars())


async def test_real_model_blocks_an_obvious_phishing_url(
    real_model: FastAPI, client: AsyncClient
) -> None:
    response = await client.post(LINKS, json={"url": PHISHING})
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "url_blocked"
    version = real_model.state.screener.model_version
    assert error["details"] == [{"model_version": version}]
    assert "score" not in response.text  # never hand attackers an oracle

    async with real_model.state.session_factory() as session:
        assert (await session.execute(select(Link))).first() is None
    [event] = await events(real_model)
    assert event.decision is ScreeningDecision.BLOCK
    assert event.link_id is None
    assert event.score >= real_model.state.screener.block_threshold
    assert event.model_version == version


async def test_real_model_allows_a_benign_url(real_model: FastAPI, client: AsyncClient) -> None:
    response = await client.post(LINKS, json={"url": BENIGN})
    assert response.status_code == 201
    body = response.json()
    assert body["screening_decision"] == "allow"
    [event] = await events(real_model)
    assert event.decision is ScreeningDecision.ALLOW
    async with real_model.state.session_factory() as session:
        link = (await session.execute(select(Link))).scalar_one()
    assert event.link_id == link.id
    assert link.model_version == real_model.state.screener.model_version
    assert link.screening_score is not None
    assert link.screening_score < real_model.state.screener.review_threshold


async def test_middle_band_creates_a_flagged_link(app: FastAPI, client: AsyncClient) -> None:
    app.dependency_overrides[get_screener] = lambda: stub_screener(default=0.7)
    response = await client.post(LINKS, json={"url": "https://example.com/borderline"})
    assert response.status_code == 201
    assert response.json()["screening_decision"] == "review"
    assert (await client.get(f"{LINKS}/{response.json()['code']}")).json()[
        "screening_decision"
    ] == "review"
    assert (await client.get(f"/{response.json()['code']}")).status_code == 302
    [event] = await events(app)
    assert event.decision is ScreeningDecision.REVIEW


async def test_every_decision_is_logged_with_the_model_version(
    app: FastAPI, client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    app.dependency_overrides[get_screener] = lambda: stub_screener(
        overrides={"https://bad.example.net/": 0.95}
    )
    with caplog.at_level(logging.INFO, logger="app.services.screening"):
        await client.post(LINKS, json={"url": "https://good.example.net/"})
        await client.post(LINKS, json={"url": "https://bad.example.net/"})
    screened = [r for r in caplog.records if r.getMessage() == "url_screened"]
    assert [r.decision for r in screened] == ["allow", "block"]  # type: ignore[attr-defined]
    assert {r.model_version for r in screened} == {"stub-model-1"}  # type: ignore[attr-defined]


async def test_alias_and_url_checks_run_before_the_model(app: FastAPI, client: AsyncClient) -> None:
    for body in [
        {"url": "http://localhost/admin"},
        {"url": "https://example.com/x", "alias": "api"},
    ]:
        assert (await client.post(LINKS, json=body)).status_code == 422
    assert await events(app) == []


async def test_inference_runs_off_the_event_loop_thread() -> None:
    seen: list[threading.Thread] = []

    class RecordingScorer(FixedScorer):
        def score(self, url: str) -> float:
            seen.append(threading.current_thread())
            return 0.0

    screener = UrlScreener(RecordingScorer(), block_threshold=0.9, review_threshold=0.5)
    await screener.screen("https://example.com/")
    assert seen
    assert seen[0] is not threading.main_thread()


def test_thresholds_default_to_the_model_metadata_and_can_be_overridden() -> None:
    default = build_screener(make_settings())
    meta_block = default.block_threshold
    assert 0 < default.review_threshold <= meta_block < 1

    tuned = build_screener(make_settings(ml_block_threshold=0.97, ml_review_threshold=0.0))
    assert (tuned.block_threshold, tuned.review_threshold) == (0.97, 0.0)


async def test_startup_fails_on_a_missing_model(tmp_path: Path) -> None:
    app = create_app(make_settings(model_path=tmp_path / "missing.joblib"))
    with pytest.raises(ModelLoadError, match="metadata"):
        async with app.router.lifespan_context(app):
            pass


async def test_startup_refuses_a_tampered_model(tmp_path: Path) -> None:
    artifact = tmp_path / "url_classifier.joblib"
    shutil.copy(MODEL_PATH, artifact)
    shutil.copy(MODEL_PATH.with_suffix(".json"), artifact.with_suffix(".json"))
    with artifact.open("ab") as handle:
        handle.write(b"tampered")
    app = create_app(make_settings(model_path=artifact))
    with pytest.raises(ModelLoadError, match="refusing to unpickle"):
        async with app.router.lifespan_context(app):
            pass
