"""Train, select, evaluate and save the malicious-URL classifier.

    uv run python -m ml.train            # or: make train

Reproducible: the dataset is pinned by checksum, every split and model uses a fixed seed,
and the script writes the model artifact, its metadata and the evaluation report together,
so the report always describes the artifact next to it.

Protocol
--------
1. Split by *registered domain* into train / validation / test (70/15/15). Many URLs share
   a domain; a random split puts siblings of the same site on both sides and rewards
   memorising sites instead of learning URL shape. The report shows how much a random
   split would have overstated performance.
2. Fit two candidates on train: logistic regression and histogram gradient boosting.
3. Select on validation PR-AUC (average precision). PR-AUC rather than ROC-AUC because the
   classes are imbalanced and what matters operationally is precision at high scores.
4. Choose thresholds on validation: ``block`` at the lowest score with precision >= 0.99;
   ``review`` at the lowest score keeping the benign flag rate (FPR) <= 2%.
5. Report everything once on the untouched test split.
"""

import argparse
import json
import logging
import os
import platform
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import numpy.typing as npt
import sklearn
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
    precision_score,
    recall_score,
    roc_auc_score,
    roc_curve,
)
from sklearn.model_selection import GroupShuffleSplit, train_test_split
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from app.ml.classifier import UrlClassifier, file_sha256, metadata_path_for
from app.ml.features import FEATURE_NAMES, NUMERIC_FEATURES, featurize, normalize_url
from app.ml.features import registered_domain as domain_of
from ml import dataset

logger = logging.getLogger("ml.train")

SEED = 42
BLOCK_MIN_PRECISION = 0.99
REVIEW_MAX_FPR = 0.02
TLD_MIN_FREQUENCY = 50

# Hand-written URLs scored in the report to show behaviour on realistic, modern inputs.
# Anecdotes, not a metric: they were not sampled from any distribution.
SANITY_URLS = {
    "benign": [
        "https://example.com",
        "https://www.bbc.co.uk",
        "https://en.wikipedia.org/wiki/Python_(programming_language)",
        "https://github.com/fastapi/fastapi/issues/1234",
        "https://docs.python.org/3/library/asyncio.html",
        "https://www.amazon.com/dp/B08N5WRWNW",
        "https://news.ycombinator.com/item?id=41234567",
        "https://accounts.google.com/signin/v2/identifier",
        "https://drive.google.com/file/d/1a2B3c4D5e6F7g8H9i0J/view?usp=sharing",
        "https://us02web.zoom.us/j/81234567890?pwd=QWERTYuiop123",
    ],
    "malicious": [
        "http://paypal.com.secure-login-verify-account.xyz/webscr/cmd=_login-submit/update.php",
        "http://198.51.100.23/bins/mirai.x86",
        "http://appleid-verify-account-locked.top/signin/index.php?session=8f7a6d5e4c3b2a1",
        "http://free-gift-card-bonus.win/claim.php?id=88823&lucky=1",
        "http://login.microsoftonline.com.office365-secure.ru/owa/auth.php",
        "http://secure-banking-update.tk/wellsfargo/verification/login.html",
    ],
}


@dataclass
class Split:
    x: npt.NDArray[np.object_]
    y: npt.NDArray[np.int_]
    urls: list[str]


def make_preprocessor(scale: bool) -> ColumnTransformer:
    numeric = list(range(len(NUMERIC_FEATURES)))
    tld = [len(NUMERIC_FEATURES)]
    return ColumnTransformer(
        [
            ("numeric", StandardScaler() if scale else "passthrough", numeric),
            (
                "tld",
                OneHotEncoder(
                    handle_unknown="infrequent_if_exist",
                    min_frequency=TLD_MIN_FREQUENCY,
                    sparse_output=False,
                ),
                tld,
            ),
        ]
    )


def candidates() -> dict[str, Pipeline]:
    return {
        "logistic_regression": Pipeline(
            [
                ("features", make_preprocessor(scale=True)),
                ("model", LogisticRegression(max_iter=2000, random_state=SEED)),
            ]
        ),
        "hist_gradient_boosting": Pipeline(
            [
                ("features", make_preprocessor(scale=False)),
                (
                    "model",
                    HistGradientBoostingClassifier(
                        max_iter=400, learning_rate=0.1, early_stopping=True, random_state=SEED
                    ),
                ),
            ]
        ),
    }


def grouped_split(urls: list[str], labels: list[int]) -> tuple[Split, Split, Split]:
    groups = np.array([domain_of(normalize_url(u).host) for u in urls])
    y = np.array(labels)
    index = np.arange(len(urls))
    outer = GroupShuffleSplit(n_splits=1, test_size=0.15, random_state=SEED)
    rest, test = next(outer.split(index, y, groups))
    inner = GroupShuffleSplit(n_splits=1, test_size=0.15 / 0.85, random_state=SEED)
    train_rel, val_rel = next(inner.split(rest, y[rest], groups[rest]))
    train, val = rest[train_rel], rest[val_rel]
    features = featurize(urls)

    def take(ids: npt.NDArray[np.int_]) -> Split:
        return Split(features[ids], y[ids], [urls[i] for i in ids])

    return take(train), take(val), take(test)


def threshold_for_precision(
    y: npt.NDArray[np.int_], scores: npt.NDArray[np.float64], min_precision: float
) -> float:
    precision, _, thresholds = precision_recall_curve(y, scores)
    for p, t in zip(precision[:-1], thresholds, strict=True):
        if p >= min_precision:
            return float(t)
    return float(thresholds[-1])


def threshold_for_fpr(
    y: npt.NDArray[np.int_], scores: npt.NDArray[np.float64], max_fpr: float
) -> float:
    fpr, _, thresholds = roc_curve(y, scores)
    allowed = thresholds[fpr <= max_fpr]
    return float(min(allowed[np.isfinite(allowed)]))


def metrics_at(
    y: npt.NDArray[np.int_], scores: npt.NDArray[np.float64], threshold: float
) -> dict[str, Any]:
    predicted = (scores >= threshold).astype(int)
    tn, fp, fn, tp = confusion_matrix(y, predicted, labels=[0, 1]).ravel()
    return {
        "threshold": round(threshold, 4),
        "precision": round(float(precision_score(y, predicted, zero_division=0)), 4),
        "recall": round(float(recall_score(y, predicted)), 4),
        "f1": round(float(f1_score(y, predicted)), 4),
        "false_positive_rate": round(float(fp / (fp + tn)), 4),
        "confusion_matrix": {"tn": int(tn), "fp": int(fp), "fn": int(fn), "tp": int(tp)},
    }


def random_split_auc(model_name: str, split_all: Split) -> dict[str, float]:
    """Same model, naive random split: shows how much domain leakage inflates metrics."""
    x_train, x_test, y_train, y_test = train_test_split(
        split_all.x, split_all.y, test_size=0.15, random_state=SEED, stratify=split_all.y
    )
    model = candidates()[model_name].fit(x_train, y_train)
    scores = model.predict_proba(x_test)[:, 1]
    return {
        "roc_auc": round(float(roc_auc_score(y_test, scores)), 4),
        "pr_auc": round(float(average_precision_score(y_test, scores)), 4),
    }


def measure_latency(classifier: UrlClassifier, urls: list[str]) -> dict[str, float]:
    for url in urls[:50]:
        classifier.score(url)  # warm-up
    timings = []
    for url in urls:
        start = time.perf_counter()
        classifier.score(url)
        timings.append((time.perf_counter() - start) * 1000)
    timings.sort()
    return {
        "p50_ms": round(statistics.median(timings), 3),
        "p99_ms": round(timings[int(len(timings) * 0.99) - 1], 3),
        "samples": len(timings),
    }


def write_report(path: Path, metrics: dict[str, Any]) -> None:
    test = metrics["test"]
    lines = [
        "# Malicious-URL classifier: evaluation report",
        "",
        "Generated by `python -m ml.train`; do not edit by hand. Model version "
        f"`{metrics['version']}`, trained {metrics['trained_at']}.",
        "",
        "## Data",
        "",
        f"- Source: public dataset `{dataset.DATA_URL}` (sha256 `{dataset.DATA_SHA256[:16]}...`).",
        "  This is real, labelled data, not synthetic. Its quality limits are listed in",
        "  [docs/ML.md](../ML.md).",
        f"- {metrics['data']['raw_rows']:,} raw rows; {metrics['data']['dropped_duplicates']:,}"
        f" duplicates and {metrics['data']['dropped_conflicts']:,} conflicting-label URLs "
        f"dropped; {metrics['data']['rows']:,} used ({metrics['data']['malicious_share']:.1%} "
        "malicious).",
        f"- Split by registered domain: train {metrics['data']['train']:,}, validation "
        f"{metrics['data']['val']:,}, test {metrics['data']['test']:,}. No domain appears in"
        " more than one split.",
        "",
        "## Model selection (validation split)",
        "",
        "| Candidate | ROC-AUC | PR-AUC | Fit time (s) |",
        "|---|---|---|---|",
    ]
    for name, cand in metrics["candidates"].items():
        lines.append(f"| {name} | {cand['roc_auc']} | {cand['pr_auc']} | {cand['fit_seconds']} |")
    lines += [
        "",
        f"Selected: **{metrics['selected_model']}** (highest validation PR-AUC).",
        "",
        "## Test split (held out, scored once)",
        "",
        f"- ROC-AUC: **{test['roc_auc']}**",
        f"- PR-AUC (average precision): **{test['pr_auc']}**",
        "",
        "| Operating point | Threshold | Precision | Recall | F1 | FPR | TN | FP | FN | TP |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for label, key in [
        ("Block (score >= block)", "at_block"),
        ("Flag or block (score >= review)", "at_review"),
        ("Naive 0.5 cut-off", "at_0_5"),
    ]:
        m = test[key]
        cm = m["confusion_matrix"]
        lines.append(
            f"| {label} | {m['threshold']} | {m['precision']} | {m['recall']} | {m['f1']} | "
            f"{m['false_positive_rate']} | {cm['tn']:,} | {cm['fp']:,} | {cm['fn']:,} | "
            f"{cm['tp']:,} |"
        )
    leak = metrics["random_split_comparison"]
    lines += [
        "",
        "Thresholds were chosen on the validation split: block = lowest score with precision"
        f" >= {BLOCK_MIN_PRECISION}; review = lowest score with benign flag rate <= "
        f"{REVIEW_MAX_FPR:.0%}.",
        "",
        "## Leakage check",
        "",
        "The same model trained and tested on a naive *random* split scores ROC-AUC "
        f"{leak['roc_auc']} / PR-AUC {leak['pr_auc']}, versus {test['roc_auc']} / "
        f"{test['pr_auc']} on the domain-grouped split above. The gap is the part of a "
        "random-split score that comes from recognising sites seen in training.",
        "",
        "## Hand-written sanity URLs (anecdotal, not a metric)",
        "",
        "| Expected | URL | Score | Decision |",
        "|---|---|---|---|",
    ]
    for row in metrics["sanity"]:
        lines.append(f"| {row['expected']} | `{row['url']}` | {row['score']} | {row['decision']} |")
    latency = metrics["latency"]
    lines += [
        "",
        "## Inference latency",
        "",
        f"Single-URL scoring (feature extraction + predict_proba), {latency['samples']} calls:"
        f" p50 {latency['p50_ms']} ms, p99 {latency['p99_ms']} ms on {metrics['machine']}.",
        "",
        "## Limitations",
        "",
        "See [docs/ML.md](../ML.md#limitations). In short: the dataset is old and noisy, the"
        " benign class is dominated by a few large sites, lexical features cannot see page"
        " content or domain age, and an attacker who knows the features can craft URLs that"
        " look benign. Treat these numbers as an upper bound on real-world performance.",
        "",
    ]
    path.write_text("\n".join(lines))


def decision_for(score: float, block: float, review: float) -> str:
    if score >= block:
        return "block"
    if score >= review:
        return "review"
    return "allow"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--data", type=Path, default=Path("ml/data/data.csv"))
    parser.add_argument("--artifact", type=Path, default=Path("artifacts/url_classifier.joblib"))
    parser.add_argument("--report-dir", type=Path, default=Path("docs/ml_eval"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    dataset.ensure_downloaded(args.data)
    data = dataset.load(args.data)
    logger.info("loaded %d urls", len(data.urls))
    train, val, test = grouped_split(data.urls, data.labels)

    results: dict[str, dict[str, Any]] = {}
    fitted: dict[str, Pipeline] = {}
    for name, pipeline in candidates().items():
        start = time.perf_counter()
        pipeline.fit(train.x, train.y)
        elapsed = time.perf_counter() - start
        scores = pipeline.predict_proba(val.x)[:, 1]
        results[name] = {
            "roc_auc": round(float(roc_auc_score(val.y, scores)), 4),
            "pr_auc": round(float(average_precision_score(val.y, scores)), 4),
            "fit_seconds": round(elapsed, 1),
        }
        fitted[name] = pipeline
        logger.info("%s: %s", name, results[name])

    selected = max(results, key=lambda n: results[n]["pr_auc"])
    model = fitted[selected]
    val_scores = model.predict_proba(val.x)[:, 1]
    block = threshold_for_precision(val.y, val_scores, BLOCK_MIN_PRECISION)
    review = min(threshold_for_fpr(val.y, val_scores, REVIEW_MAX_FPR), block)

    test_scores = model.predict_proba(test.x)[:, 1]
    trained_at = datetime.now(UTC).replace(microsecond=0)

    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(model, args.artifact, compress=3)
    artifact_sha = file_sha256(args.artifact)
    version = f"urlclf-{trained_at:%Y%m%d}-{artifact_sha[:8]}"
    metadata: dict[str, Any] = {
        "version": version,
        "artifact_sha256": artifact_sha,
        "trained_at": trained_at.isoformat(),
        "model": selected,
        "features": list(FEATURE_NAMES),
        "thresholds": {"block": round(block, 6), "review": round(review, 6)},
        "dataset": {"url": dataset.DATA_URL, "sha256": dataset.DATA_SHA256},
        "sklearn_version": sklearn.__version__,
        "python_version": platform.python_version(),
        "seed": SEED,
    }
    metadata_path_for(args.artifact).write_text(json.dumps(metadata, indent=2) + "\n")

    classifier = UrlClassifier.load(args.artifact)
    all_split = Split(
        np.concatenate([train.x, val.x, test.x]),
        np.concatenate([train.y, val.y, test.y]),
        train.urls + val.urls + test.urls,
    )
    metrics: dict[str, Any] = {
        "version": version,
        "trained_at": trained_at.isoformat(),
        "machine": f"{platform.machine()} {platform.system()}, {_cpu_count()} vCPU, Python "
        f"{platform.python_version()}, scikit-learn {sklearn.__version__}",
        "data": {
            "raw_rows": data.raw_rows,
            "dropped_duplicates": data.dropped_duplicates,
            "dropped_conflicts": data.dropped_conflicts,
            "rows": len(data.urls),
            "malicious_share": float(np.mean(data.labels)),
            "train": len(train.y),
            "val": len(val.y),
            "test": len(test.y),
        },
        "candidates": results,
        "selected_model": selected,
        "thresholds": metadata["thresholds"],
        "test": {
            "roc_auc": round(float(roc_auc_score(test.y, test_scores)), 4),
            "pr_auc": round(float(average_precision_score(test.y, test_scores)), 4),
            "at_block": metrics_at(test.y, test_scores, block),
            "at_review": metrics_at(test.y, test_scores, review),
            "at_0_5": metrics_at(test.y, test_scores, 0.5),
        },
        "random_split_comparison": random_split_auc(selected, all_split),
        "sanity": [
            {
                "expected": expected,
                "url": url,
                "score": round(classifier.score(url), 4),
                "decision": decision_for(classifier.score(url), block, review),
            }
            for expected, urls in SANITY_URLS.items()
            for url in urls
        ],
        "latency": measure_latency(classifier, test.urls[:2000]),
    }
    args.report_dir.mkdir(parents=True, exist_ok=True)
    (args.report_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    write_report(args.report_dir / "report.md", metrics)
    logger.info("saved %s (%s); test %s", args.artifact, version, metrics["test"]["pr_auc"])


def _cpu_count() -> int:
    return os.cpu_count() or 1


if __name__ == "__main__":
    main()
