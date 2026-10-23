"""Malicious-URL screening at link-creation time.

Scores are mapped to three outcomes by two configurable thresholds:

    score >= block_threshold   -> block   (422, link not created)
    score >= review_threshold  -> review  (link created, flagged for a human)
    otherwise                  -> allow

Two thresholds instead of one because the costs are asymmetric. Blocking a legitimate URL
is a visible failure for a customer, so it needs very high confidence. Letting a bad URL
through is also costly, but a middle band that is created-and-flagged lets humans catch
those without blocking anyone. The defaults come from the model's metadata, where training
chose them on validation data; environment variables can override them per deployment.
"""

import logging
from dataclasses import dataclass
from urllib.parse import urlsplit

import anyio.to_thread

from app.ml.classifier import UrlScorer
from app.models.screening import ScreeningDecision

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ScreeningResult:
    decision: ScreeningDecision
    score: float
    model_version: str


class UrlScreener:
    def __init__(self, scorer: UrlScorer, block_threshold: float, review_threshold: float) -> None:
        if not 0.0 <= review_threshold <= block_threshold <= 1.0:
            raise ValueError("thresholds must satisfy 0 <= review <= block <= 1")
        self._scorer = scorer
        self.block_threshold = block_threshold
        self.review_threshold = review_threshold

    @property
    def model_version(self) -> str:
        return self._scorer.version

    def decide(self, score: float) -> ScreeningDecision:
        if score >= self.block_threshold:
            return ScreeningDecision.BLOCK
        if score >= self.review_threshold:
            return ScreeningDecision.REVIEW
        return ScreeningDecision.ALLOW

    async def screen(self, url: str) -> ScreeningResult:
        """Score ``url`` in a worker thread so the event loop keeps serving other requests.

        Model inference is CPU-bound (milliseconds here). Called directly in an ``async``
        handler it would freeze every concurrent request, including redirects, for that
        long. A thread is enough because scikit-learn's numeric code releases the GIL for
        much of the work; a process pool or a separate model server is the next step if
        inference ever becomes heavy.
        """
        score = await anyio.to_thread.run_sync(self._scorer.score, url)
        result = ScreeningResult(self.decide(score), score, self._scorer.version)
        logger.info(
            "url_screened",
            extra={
                "decision": result.decision.value,
                "score": round(score, 4),
                "model_version": result.model_version,
                "host": urlsplit(url).hostname,
                "block_threshold": self.block_threshold,
                "review_threshold": self.review_threshold,
            },
        )
        return result
