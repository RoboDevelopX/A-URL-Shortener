"""Download, verify and clean the public labelled URL dataset.

Source: https://github.com/faizann24/Using-machine-learning-to-detect-malicious-URLs
(``data/data.csv``, ~420k URLs labelled ``good`` / ``bad``). The file is pinned by SHA-256 so
every training run sees byte-identical data; a changed upstream file stops the run instead
of silently producing a different model.
"""

import csv
import hashlib
import html
import logging
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from app.ml.features import normalize_url

logger = logging.getLogger(__name__)

DATA_URL = (
    "https://raw.githubusercontent.com/faizann24/"
    "Using-machine-learning-to-detect-malicious-URLs/master/data/data.csv"
)
DATA_SHA256 = "3243f054f5205e352f7a9adc2d7b993d516acd4ad17cb032b91c989138e34808"
LABELS = {"good": 0, "bad": 1}


@dataclass(frozen=True)
class Dataset:
    urls: list[str]
    labels: list[int]
    raw_rows: int
    dropped_duplicates: int
    dropped_conflicts: int


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_downloaded(path: Path) -> None:
    """Download the CSV if absent, then verify its checksum."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        logger.info("downloading %s", DATA_URL)
        with urllib.request.urlopen(DATA_URL, timeout=120) as response:
            path.write_bytes(response.read())
    actual = sha256_of(path)
    if actual != DATA_SHA256:
        raise RuntimeError(f"{path} has sha256 {actual}, expected {DATA_SHA256}")


def load(path: Path) -> Dataset:
    """Read, clean and de-duplicate the dataset.

    Cleaning steps, each fixing an artefact of how the data was scraped:
    - HTML entities (``&amp;``) are unescaped: they appear in the malicious rows only, so a
      model would learn "contains amp;" as a malware signal.
    - Rows are de-duplicated on the *normalised* URL (no scheme, no ``www.``), the same form
      the features see, so the same URL cannot land in both train and test.
    - URLs that appear with both labels are dropped as unresolvable label noise.
    """
    by_key: dict[str, tuple[str, set[int]]] = {}
    raw_rows = 0
    with path.open(encoding="utf-8", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle):
            raw_rows += 1
            label = LABELS.get(row.get("label", "").strip())
            url = html.unescape(row.get("url", "")).strip()
            if label is None or not url:
                continue
            parts = normalize_url(url)
            key = parts.host + parts.rest
            if key in by_key:
                by_key[key][1].add(label)
            else:
                by_key[key] = (url, {label})

    urls: list[str] = []
    labels: list[int] = []
    conflicts = 0
    for url, label_set in by_key.values():
        if len(label_set) > 1:
            conflicts += 1
            continue
        urls.append(url)
        labels.append(next(iter(label_set)))
    return Dataset(
        urls=urls,
        labels=labels,
        raw_rows=raw_rows,
        dropped_duplicates=raw_rows - len(by_key),
        dropped_conflicts=conflicts,
    )
