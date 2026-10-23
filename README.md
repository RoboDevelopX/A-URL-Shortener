# URL Shortener with ML Screening

A production-style URL shortener built with FastAPI, PostgreSQL and Redis. It screens every new link with a malicious-URL classifier trained on public data.

The project is meant to be read as much as run. Each design decision is explained where it lives (in docstrings and comments), and the bigger trade-offs are written up in [docs/DESIGN.md](docs/DESIGN.md) and [docs/ML.md](docs/ML.md).

**What it does**

- `POST /api/v1/links` creates a short link, with an optional custom alias and expiry.
  - It validates the destination: http/https only, and no localhost, private or encoded IPs, or internal hostnames.
  - It then scores the URL with the classifier and either **blocks** it, **flags** it for review, or **allows** it.
- `GET /{code}` answers with a 302 redirect. It looks in Redis first and falls back to Postgres. Unknown codes return 404; expired or disabled links return 410.
- Click analytics never slow the redirect. A click is queued on a Redis Stream after the response is sent, and a separate worker batch-writes the queue into Postgres.
- `GET /api/v1/links/{code}/stats` returns total clicks, a daily series, top referrers, a device breakdown and top browsers.
- API keys are stored only as HMAC hashes. Each key manages only its own links. Redirects are public.
- Link creation is rate-limited per key and per IP with an atomic sliding window (a Lua script in Redis). Over the limit, the API returns 429 with `Retry-After`.
- Short codes are `base62(permutation(sequence id))`, so they **cannot collide** by construction (see [DESIGN.md](docs/DESIGN.md#short-code-generation)).
- If Redis goes down, the service degrades instead of failing. Redirects, link creation and analytics keep working from Postgres, and the outage is logged.

## Architecture

```mermaid
flowchart LR
    browser["Browser<br/>GET /{code}"] --> api
    client["API client<br/>X-API-Key"] --> api
    api["FastAPI app<br/>routers / services / repositories"]
    api -- "cache-aside lookups<br/>rate-limit Lua<br/>XADD click events" --> redis[("Redis")]
    api -- "links, api_keys,<br/>screening_events" --> pg[("PostgreSQL")]
    api -- "score in worker thread" --> model[["Classifier artifact<br/>(sha256-verified)"]]
    worker["Click worker<br/>python -m app.worker"] -- "XREADGROUP / XACK" --> redis
    worker -- "batch INSERT clicks<br/>(idempotent)" --> pg
    migrate["migrate<br/>alembic upgrade head"] -. "runs once before api/worker" .-> pg
```

The code is layered as `routers` (HTTP only) → `services` (business rules, raise domain errors) → `repositories` (all SQL). Below them, `models` holds the ORM tables, `schemas` the Pydantic I/O models, and `core` the config, logging, errors, DB, Redis and security. FastAPI dependency injection wires everything per request (`app/api/deps.py`), and the tests swap in fakes through `dependency_overrides`.

## Quickstart

You need Docker with Compose v2. For tests, lint and training you also need [uv](https://docs.astral.sh/uv/).

```bash
docker compose up -d --build --wait   # or: make up
# postgres + redis -> migrate (one-shot) -> api + worker; waits until healthy

KEY=$(make -s demo-key)               # issues an API key via the admin CLI
```

The API is now at http://localhost:8000, with interactive docs at http://localhost:8000/docs.

| Command | What it does |
|---|---|
| `make up` / `make down` | Start / stop the stack (`make down V=1` also drops volumes) |
| `make migrate` | Apply Alembic migrations |
| `make test` | Start Postgres + Redis, run unit and integration tests with coverage (fails under 85%) |
| `make lint` | Ruff lint, Ruff format check, mypy (strict) |
| `make train` | Download the checksum-pinned dataset, train, evaluate, write the model and report |
| `make loadtest` | Locust, headless, against the running stack |
| `make demo-key` | Print a new API key |

## Using the API

```bash
# Create a link (generated code)
curl -s -X POST localhost:8000/api/v1/links \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"url": "https://en.wikipedia.org/wiki/URL_shortening"}'
# {"code":"vjmosjL","short_url":"http://localhost:8000/vjmosjL","long_url":"https://en.wikipedia.org/wiki/URL_shortening",
#  "is_custom":false,"status":"active","screening_decision":"allow","expires_at":null,"created_at":"..."}

# Custom alias with an expiry
curl -s -X POST localhost:8000/api/v1/links \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"url": "https://docs.python.org/3/", "alias": "pydocs", "expires_at": "2027-01-01T00:00:00Z"}'

# Follow it (public, no key): 302 to the destination
curl -si localhost:8000/pydocs | head -4

# Metadata, change expiry (null removes it), disable (redirect then returns 410)
curl -s localhost:8000/api/v1/links/pydocs -H "X-API-Key: $KEY"
curl -s -X PATCH localhost:8000/api/v1/links/pydocs -H "X-API-Key: $KEY" \
  -H 'Content-Type: application/json' -d '{"expires_at": null}'
curl -s -X DELETE localhost:8000/api/v1/links/pydocs -H "X-API-Key: $KEY" -w '%{http_code}\n'

# Stats (clicks land about a second after the redirect, via the worker)
curl -s 'localhost:8000/api/v1/links/vjmosjL/stats?days=7' -H "X-API-Key: $KEY"

# The classifier blocks an obviously malicious URL
curl -s -X POST localhost:8000/api/v1/links \
  -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"url": "http://paypal.com.secure-login-verify-account.xyz/webscr/cmd=_login-submit/update.php"}'
# {"error":{"code":"url_blocked","message":"This URL was rejected by automated safety screening.",
#  "request_id":"...","details":[{"model_version":"urlclf-20261023-1d614354"}]}}
```

Every error uses the same envelope: `{"error": {"code", "message", "request_id", "details?"}}`. The `request_id` is also returned in the `X-Request-ID` header and appears on every JSON log line for that request.

| Status | `code` | When |
|---|---|---|
| 401 | `unauthorized` | Missing, unknown or revoked API key |
| 404 | `not_found` | Unknown code, or a link owned by another key |
| 409 | `alias_conflict` | Alias already used (including by a disabled link: codes are never reused) |
| 410 | `link_gone` | Redirect to an expired or disabled link; PATCH on a disabled link |
| 422 | `invalid_url`, `invalid_alias`, `invalid_expiry`, `url_blocked`, `validation_error` | Bad input, or the classifier blocked the URL |
| 429 | `rate_limited` | Creation limit hit; see `Retry-After` |

API keys are managed from a shell on the deployment, not over HTTP:

```bash
docker compose exec api python -m app.cli create-api-key --name "marketing site"
docker compose exec api python -m app.cli revoke-api-key --prefix usk_AbCdEfGh
```

## Configuration

All configuration comes from environment variables (`.env` is read in development; see [.env.example](.env.example)). The variables marked required are secrets and have no default in code. `docker-compose.yml` supplies clearly marked development-only values so the stack starts without any setup.

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | required | `postgresql+asyncpg://user:pass@host:5432/db` |
| `REDIS_URL` | required | `redis://host:6379/0` |
| `CODE_SECRET` | required | Key for the short-code permutation. Set once; never rotate. |
| `API_KEY_PEPPER` | required | HMAC key for API key hashes. Rotating it invalidates every key. |
| `PUBLIC_BASE_URL` | `http://localhost:8000` | Origin used to build `short_url`; its host is also refused as a destination |
| `CODE_LENGTH` | `7` | Generated code length (62^7 ≈ 3.5 trillion codes) |
| `MAX_URL_LENGTH` | `2048` | Longest accepted destination |
| `LOG_LEVEL` | `INFO` | `DEBUG` / `INFO` / `WARNING` / `ERROR` |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | `10` / `10` | SQLAlchemy pool per process |
| `REDIS_SOCKET_TIMEOUT_SECONDS` | `0.25` | Fail fast to the Postgres fallback |
| `REDIS_CIRCUIT_COOLDOWN_SECONDS` | `5` | How long to skip Redis after a failure |
| `CACHE_TTL_SECONDS` | `600` | Redirect cache TTL; also the upper bound on staleness if an invalidation is lost |
| `NEGATIVE_CACHE_TTL_SECONDS` | `30` | TTL for cached 404s |
| `RATE_LIMIT_WINDOW_SECONDS` | `60` | Sliding window size |
| `RATE_LIMIT_CREATE_PER_KEY` | `60` | Creates per key per window |
| `RATE_LIMIT_CREATE_PER_IP` | `120` | Creates per client IP per window |
| `CLICK_STREAM_KEY` / `CLICK_CONSUMER_GROUP` | `clicks:stream` / `click-writers` | Redis Stream for click events |
| `CLICK_STREAM_MAX_LENGTH` | `500000` | Safety cap if the worker falls behind |
| `WORKER_BATCH_SIZE` | `500` | Events per insert batch |
| `WORKER_BLOCK_MS` | `1000` | Worker poll wait |
| `WORKER_CLAIM_IDLE_MS` | `60000` | Re-claim events held by a dead worker after this long |
| `MODEL_PATH` | `artifacts/url_classifier.joblib` | Classifier; its `.json` metadata must sit next to it |
| `ML_BLOCK_THRESHOLD` | from model metadata (0.925) | Score at or above which creation is blocked |
| `ML_REVIEW_THRESHOLD` | from model metadata (0.721) | Score at or above which a link is flagged for review |

Behind a load balancer, run uvicorn with `--proxy-headers --forwarded-allow-ips=<balancer IP>` so that per-IP rate limits see the real client.

## Tests and quality

```bash
make test   # 214 tests: unit (pure functions) + integration (real Postgres and Redis)
make lint   # ruff check, ruff format --check, mypy --strict
```

The integration tests run the real app in-process against real Postgres and Redis. Before the suite, a session fixture migrates the test database down to base and back up to head, so both migration directions are exercised. Last measured run: **214 passed, 95.6% line+branch coverage of `app/`**.

What's covered:

- Collision-freedom: an exhaustive bijection check on a small code space, a 100k-id sample, a generated code that lands on an existing alias, and 40 concurrent creates.
- URL validation: about 45 accept/reject cases, including `http://2130706433/`, `[::ffff:127.0.0.1]` and `169.254.169.254`.
- Cache hit and miss, proven by deleting the row behind the cache.
- 410 for expired and disabled links, 409 for alias conflicts, 429 with `Retry-After`.
- Exact analytics counts, including redelivered events counted once and messages re-claimed from a dead worker.
- 401 on every management route, and 404 for other owners' links.
- Classifier block and allow with the real model; the review band with a stub.
- Redis outage paths for cache, rate limiter and analytics.
- Startup refusing a missing or tampered model.

CI (`.github/workflows/ci.yml`) has three jobs:

1. Lint.
2. Tests against Postgres and Redis service containers, plus `alembic upgrade`, `alembic check` (models match migrations) and `downgrade base`.
3. A Docker build and compose smoke test: create, redirect, and block. The workflow has not run on GitHub yet; I ran its smoke-test commands by hand against the local stack.

## Load test (measured)

`make loadtest` runs Locust with a read-heavy mix: about 93% redirects, 5% stats reads and 2% creates. Each simulated user sends its next request as soon as the last one returns. Only numbers I actually measured are listed.

**Machine:** one cloud VM, Intel Xeon @ 2.10 GHz, 4 vCPU, 15 GB RAM, Linux, Docker 29. Postgres, Redis, the API (2 uvicorn workers), the click worker *and* Locust all shared those 4 vCPUs.

| Run | Endpoint | Requests | Throughput | p50 | p95 | p99 | Failures |
|---|---|---|---|---|---|---|---|
| 10 users, 30 s | `GET /{code}` | 20,690 | 689 req/s | 10 ms | 22 ms | 33 ms | 0 |
| 50 users, 60 s | `GET /{code}` | 41,660 | 694 req/s | 52 ms | 100 ms | 170 ms | 0 |
| 50 users, 60 s | `GET .../stats` | 2,117 | 35 req/s | 130 ms | 250 ms | 370 ms | 0 |
| 50 users, 60 s | `POST /api/v1/links` (201 or expected 429) | 1,050 | 17 req/s | 57 ms | 190 ms | 500 ms | 0 |

What the numbers mean:

- Throughput was the same at 10 and 50 users. The two API worker processes each sat at about 90% CPU, so the API was CPU-bound on this box. The extra latency at 50 users is queueing, not slower work. Adding uvicorn workers or replicas is the scaling lever (see [DESIGN.md](docs/DESIGN.md#horizontal-scaling)).
- The click pipeline kept up. After the 50-user run, Postgres held exactly one click row per redirect (41,660 plus 21 earlier clicks), with zero stream lag and zero pending messages.
- These figures come from a small, shared machine with the load generator on the same host. They show relative behaviour, not production capacity.

## Project layout

```
app/
  core/          config, JSON logging, error schema, DB engine, Redis gateway, API-key hashing
  models/        links, clicks, api_keys, screening_events
  schemas/       Pydantic request/response models
  repositories/  SQL (SQLAlchemy, always parameterised)
  services/      codes, url_validation, links, redirects, cache, analytics, stats, rate_limit, auth, screening
  routers/       health, links (management), redirect
  api/deps.py    dependency injection
  ml/            features (shared by training and serving), classifier loading
  worker.py      click-ingestion worker      cli.py  API key admin
migrations/      Alembic (0001 links, 0002 clicks, 0003 api keys, 0004 screening)
ml/              dataset download/cleaning, training + evaluation
artifacts/       committed model + metadata (sha256-verified at load)
docs/            DESIGN.md, ML.md, ml_eval/report.md (generated)
tests/           unit/ and integration/
loadtest/        locustfile.py
```

## Known limitations

- **The classifier is the weakest part, and it says so.** It catches obvious phishing-shaped URLs. It also blocks `https://accounts.google.com/signin/v2/identifier`, a genuine sign-in page. Its test metrics come from a 2017-era dataset and are an upper bound on real-world performance. See [docs/ML.md](docs/ML.md#limitations).
- Analytics are eventually consistent, typically about a second behind.
- Stats queries aggregate raw click rows. That is fine up to millions of clicks per link; beyond that they need rollups (see DESIGN.md).
- Abuse controls beyond creation limits, such as redirect-side rate limits and bot filtering in stats, belong at the edge and are not built here.
