# Malicious-URL screening

At creation time, every destination URL is scored by a small classifier that looks at **the URL string only**. A score of 0.925 or higher blocks the link. A score from 0.721 up to 0.925 creates the link but flags it for review. Anything lower is allowed.

This document covers the data, the features, the model, how the thresholds were picked, and where the model breaks. The generated evaluation, with every number below, is in [ml_eval/report.md](ml_eval/report.md) and [ml_eval/metrics.json](ml_eval/metrics.json).

> **Read this first.** The numbers below are real measurements on a real public dataset. They are **not** an estimate of production performance. The dataset is old, noisy and unrepresentative of today's traffic, and the evaluation can only measure agreement with its labels. Section 7 lists the weaknesses, including a legitimate Google sign-in URL that the model blocks.

## 1. Dataset

- **Source:** [`faizann24/Using-machine-learning-to-detect-malicious-URLs`](https://github.com/faizann24/Using-machine-learning-to-detect-malicious-URLs), `data/data.csv`. That is 420,464 URLs labelled `good` or `bad`, compiled around 2017 from public blocklists and crawls.
- **Why this one:** it was the public labelled set reachable from the build environment. UCI's PhiUSIIL, URLhaus and the Tranco list were blocked by the network policy there. No synthetic data was used anywhere.
- **Reproducibility:** `ml/dataset.py` downloads the file and refuses to train unless its SHA-256 is `3243f054…e34808`. Splits and models use seed 42. Re-running `make train` produced a byte-identical artifact (checked).

### Cleaning, and three shortcuts the model would otherwise learn

Before trusting any metric, I checked whether the labels could be predicted from artefacts of how the data was collected. Three such artefacts turned up:

| Artefact | Benign | Malicious | Fix (in `normalize_url`, shared with serving) |
|---|---|---|---|
| URL has a scheme (`http://`) | 0% | 0.14% | Drop the scheme |
| Starts with `www.` | 0% | 2.9% | Drop `www.` |
| Bare domain with no `/` at all | 0 rows | 2,102 rows | Treat an empty path as `/` (RFC 3986 says they are equal) |
| HTML entities (`&amp;`) | rare | common | `html.unescape` during cleaning |

The third was found the hard way. The first trained model scored `https://example.com` at **0.998**, because in this data "no trailing slash" almost always meant malicious. After the fix it scores 0.066.

This is the most transferable lesson in the project: **check what your model can cheat with before you look at its accuracy**. A model that keys on how the data was scraped will look excellent offline and fail on live traffic.

After cleaning and de-duplicating on the normalised URL, and dropping 2 URLs that appeared with both labels, **410,947 URLs** remain, 16.1% of them malicious.

### Splitting by domain

Many URLs share a site: thousands of `en.wikipedia.org/wiki/...` pages, many paths on one phishing host. A random split puts siblings in both train and test, and the model gets rewarded for recognising sites instead of judging URL shape.

The data is therefore split by **registered domain** with `GroupShuffleSplit`: train 304,315 / validation 55,579 / test 51,053, with no domain in more than one split. As a leakage check, the same model on a naive random split scores ROC-AUC 0.977 and PR-AUC 0.934, against 0.962 and 0.902 on the grouped split. The difference is how much a random split would have flattered the model.

## 2. Features

There are 27 numeric features plus the TLD, all computed from the string (`app/ml/features.py`). **No network calls:** no DNS, no WHOIS, no page fetches. Scoring therefore takes milliseconds, needs no egress, and cannot be abused to make the server contact an attacker's host.

| Group | Features |
|---|---|
| Length / shape | URL, host, path and query length; path depth; query parameter count; longest alphanumeric token |
| Character mix | digit ratio, special-character ratio, uppercase ratio, `%`-encodings, Shannon entropy of the URL and of the host |
| Host structure | dots, hyphens and digits in the host; subdomain count (vs. an approximate registered domain); IP-literal host; explicit port; punycode (`xn--`); longest consonant run (random-looking DGA hosts) |
| Tricks | `@` in the URL; `//` inside the path; known shortener host |
| Vocabulary | count of suspicious words (`login`, `verify`, `account`, `webscr`, `wp-admin`, ...); a brand name (`paypal`, `apple`, ...) appearing *outside* the registered domain; executable file extension |
| Categorical | TLD, one-hot encoded; TLDs seen fewer than 50 times share an "infrequent" bucket, and unseen TLDs map to it too |

**Train/serve skew** is prevented structurally: `ml/train.py` and the API import the same `extract_features` and `normalize_url`. There is no second implementation to drift.

## 3. Model choice

Two candidates were fitted on train and compared on validation:

| Model | Val ROC-AUC | Val PR-AUC | Fit time |
|---|---|---|---|
| Logistic regression (scaled features) | 0.885 | 0.764 | 11 s |
| **HistGradientBoosting** | **0.951** | **0.882** | 37 s |

- **Selection metric:** gradient boosting won on validation PR-AUC. PR-AUC (average precision) was the selection metric rather than ROC-AUC because the classes are imbalanced (16% positive), and what matters operationally is precision at high scores. ROC-AUC is dominated by the many easy negatives.
- **Why boosting fits here:** the features interact strongly. "Many hyphens" means little alone and a lot in a host with a brand name and an odd TLD. Trees capture those interactions; a linear model cannot.
- **Cost:** it is still a scikit-learn pipeline (`ColumnTransformer` → `HistGradientBoostingClassifier`), a 650 KB artifact, with **measured** single-URL latency of p50 5.1 ms and p99 20 ms, including feature extraction.
- **What wasn't tried:** character-level CNNs or URL transformers would likely score better, but they need more data than this set honestly supports, a GPU or a model server, and far more effort to explain. They are the next step if real traffic and labels exist (section 8).

## 4. Results on the held-out test split

Test ROC-AUC is **0.962** and PR-AUC is **0.902**. The test split was scored once, after the model and thresholds were fixed.

| Operating point | Threshold | Precision | Recall | F1 | FPR | TN | FP | FN | TP |
|---|---|---|---|---|---|---|---|---|---|
| Block | 0.925 | 0.989 | 0.551 | 0.708 | 0.13% | 41,551 | 56 | 4,238 | 5,208 |
| Flag or block | 0.721 | 0.948 | 0.699 | 0.805 | 0.88% | 41,241 | 366 | 2,841 | 6,605 |
| Naive 0.5 | 0.500 | 0.839 | 0.775 | 0.805 | 3.38% | 40,199 | 1,408 | 2,128 | 7,318 |

Read plainly:

- Of the URLs the service blocks, about 99% are labelled malicious. The block band catches 55% of malicious URLs.
- Together, the flag and block bands catch 70% of malicious URLs while flagging under 1% of benign ones.
- About 30% of malicious URLs in the test set score low enough to be allowed. **The classifier is a filter, not a guarantee.**

## 5. Threshold selection

The thresholds were chosen on the validation split and stored in the model's metadata (`artifacts/url_classifier.json`). The API uses them unless `ML_BLOCK_THRESHOLD` or `ML_REVIEW_THRESHOLD` overrides them.

- **Block = the lowest score at which validation precision ≥ 0.99.** A wrongly blocked link is a visible failure for a legitimate customer, with no workaround. So blocking is reserved for very confident scores, accepting that recall in this band is only about 55%.
- **Review = the lowest score at which the benign flag rate (FPR) ≤ 2% on validation.** Flagged links still work, so a false flag costs reviewer time, not customer pain. The constraint is set by reviewer capacity: at 10 M creates a month, 2% of benign creates would be about 170 k reviews. That is why it should be tuned to the real queue. It landed at 0.72, which gave 0.9% FPR on test.
- **Two thresholds instead of one** because the two errors have very different costs. A single cut-off at 0.5 would block 3.4% of benign URLs.

Both thresholds should be re-chosen with every retrain. They belong to a specific model, not to the service.

## 6. How it runs in the service

- **Startup:** the API loads the artifact and **verifies its SHA-256 against the metadata before unpickling** (unpickling runs code). It refuses to start if the artifact is missing or modified.
- **Off the event loop:** scoring runs via `anyio.to_thread.run_sync`, so the CPU work never blocks the event loop; redirects keep flowing while a create is being scored.
- **Order of checks:** URL validation, alias rules and expiry are checked before the model, so cheap deterministic rejections never cost an inference.
- **Logging:** every decision is logged as JSON with `decision`, `score`, `model_version`, the host and both thresholds. Each one is also stored in `screening_events`, blocks included.
- **No score oracle:** a blocked create returns 422 `url_blocked` with the model version but **no score**. Returning scores would give an attacker an oracle to tune a URL until it slips under the threshold.

## 7. Limitations

Be explicit about these in an interview; they are the interesting part.

1. **Known false positive.** `https://accounts.google.com/signin/v2/identifier` scores 0.94 and is **blocked**. The model learned that `accounts`, `signin` and deep paths look phishy, and lexical features cannot tell Google's real sign-in page from an imitation. A production system would put an allowlist of high-reputation registered domains in front of the model, and feed reviewed false positives back into training.
2. **Old, noisy labels.**
   - The data is around 2017. Phishing has since shifted to free hosting, cloud storage links, compromised legitimate sites, and URL shorteners.
   - Some "malicious" rows are just domains that were on a blocklist at some point.
   - The benign class is dominated by a few big sites (Wikipedia, YouTube, Facebook, Amazon) and by crawl paths, not by the links people actually share.
3. **The metrics measure agreement with these labels, not real-world accuracy.** Even with the domain-grouped split, the test set comes from the same collection process as the training set. Expect lower precision and recall on live traffic.
4. **Lexical features have a ceiling.**
   - They cannot see page content, domain age, certificates, hosting reputation or redirect chains.
   - A malicious page on a clean-looking domain (`https://docs-share.com/d/abc`), or on a compromised legitimate site, will be allowed.
5. **Adversarial evasion is easy for a motivated attacker** who knows or guesses the features: short paths, no suspicious words, a common TLD. Hiding the score helps a little; it does not make the model robust.
6. **Heuristic registered-domain detection.** A small second-level list is used instead of the full Public Suffix List, so some hosts get their subdomain count wrong.
7. **Scores are not calibrated probabilities.** They rank URLs well, but "0.8" does not mean an 80% chance of being malicious. Use them through the thresholds, not as probabilities.

## 8. Retraining and monitoring in production

**What to monitor** (all derivable from `screening_events` and the logs):

- **Decision rates over time:** the block and flag shares per day. A sudden jump usually means a new spam campaign, or a model or data bug, not a change in the world.
- **Score distribution drift:** compare each day's score histogram with the training-time distribution using the Population Stability Index or a KS test. Alert at PSI > 0.2.
- **Feature drift:** the same comparison per feature, especially TLD mix and URL length. This explains *why* scores moved.
- **Latency of `url_screened`** and model load failures at deploy.
- **Ground truth as it arrives:**
  - Reviewer verdicts on flagged links.
  - Appeals of blocks (false positives).
  - Links later reported or found on external feeds such as URLhaus or Safe Browsing (false negatives).
  - Join these back to `screening_events` by link or URL to track live precision and recall per model version.

**False-positive review loop**

1. Reviewers work the `review` queue (the partial index on `screening_events`) and sample `block` decisions.
2. Each verdict becomes a label. Confirmed false positives also feed the domain allowlist immediately, without waiting for a retrain.
3. Track the false-positive rate per model version. A new model must not regress on the reviewed set.

**Retraining**

- **When:** on a schedule (monthly), or when drift alerts fire.
- **Data:** the public data, plus reviewer labels, plus fresh positives from threat feeds, plus a sample of recent allowed links as benign. Weight recent data more. Keep the domain-grouped split, and also hold out the most recent time window so the test mimics deployment.
- **Process:** `make train` is the entry point, extended with the new sources. It writes the artifact, the metadata (with the thresholds re-chosen) and the report together, versioned as `urlclf-<date>-<sha>`.
- **Promotion:**
  1. **Shadow** the new model: score every create with both models, act on the old one, and log both.
  2. Compare decision rates and the reviewed-set false-positive rate.
  3. Then switch by changing `MODEL_PATH`.
  4. Because each decision row stores `model_version`, any incident can be traced to the exact model that made the call, and rolling back is a config change.
