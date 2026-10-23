"""Loading and running the malicious-URL model.

The artifact is a scikit-learn pipeline saved with joblib next to a JSON metadata file.
Unpickling executes code, so an artifact is only loaded if its SHA-256 matches the one
recorded in the metadata at training time: a corrupted or swapped file fails at startup
instead of serving wrong scores. (This is an integrity check, not a signature; in a real
deployment artifacts would come from a model registry with access control.)
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import joblib

from app.ml.features import featurize


class ModelLoadError(RuntimeError):
    """The artifact is missing, unreadable, or does not match its metadata."""


class UrlScorer(Protocol):
    """Anything that turns a URL into a malicious-probability. Tests use fakes of this."""

    @property
    def version(self) -> str: ...

    def score(self, url: str) -> float: ...


@dataclass(frozen=True)
class ModelMetadata:
    version: str
    artifact_sha256: str
    block_threshold: float
    review_threshold: float


def metadata_path_for(artifact_path: Path) -> Path:
    return artifact_path.with_suffix(".json")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class UrlClassifier:
    """A loaded model. ``score`` is CPU-bound; call it off the event loop."""

    def __init__(self, pipeline: Any, metadata: ModelMetadata) -> None:
        self._pipeline = pipeline
        self.metadata = metadata

    @property
    def version(self) -> str:
        return self.metadata.version

    @classmethod
    def load(cls, artifact_path: Path) -> "UrlClassifier":
        meta_path = metadata_path_for(artifact_path)
        try:
            raw = json.loads(meta_path.read_text())
            metadata = ModelMetadata(
                version=str(raw["version"]),
                artifact_sha256=str(raw["artifact_sha256"]),
                block_threshold=float(raw["thresholds"]["block"]),
                review_threshold=float(raw["thresholds"]["review"]),
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ModelLoadError(f"cannot read model metadata {meta_path}: {exc}") from exc
        try:
            actual = file_sha256(artifact_path)
        except OSError as exc:
            raise ModelLoadError(f"cannot read model artifact {artifact_path}: {exc}") from exc
        if actual != metadata.artifact_sha256:
            raise ModelLoadError(
                f"{artifact_path} sha256 {actual} does not match metadata "
                f"{metadata.artifact_sha256}; refusing to unpickle it"
            )
        return cls(joblib.load(artifact_path), metadata)

    def score(self, url: str) -> float:
        """Probability-like score in [0, 1] that ``url`` is malicious."""
        return float(self._pipeline.predict_proba(featurize([url]))[0, 1])
