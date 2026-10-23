# Design

This document explains *why* the service is built the way it is. Where a decision has a cost, the cost is stated. Numbers marked **measured** come from runs described in the README; everything else is an estimate with its assumptions shown.

## 1. Requirements

**Functional**

- Create a short link for an http/https URL, with an optional custom alias and expiry. Two different links must never share a code.
- Redirect `GET /{code}` with a 302. Unknown codes return 404; expired or disabled links return 410.
- Read link metadata, change the expiry, and disable links. Only the link's owner (its API key) may do these.
- Record clicks (time, referrer, parsed user agent) and serve per-link stats.
- Screen destinations for malicious URLs at creation: block, flag for review, or allow.

**Non-functional**

- The redirect is the hot path. It must be fast (one Redis round trip on a cache hit) and must never wait on analytics.
- Reads vastly outnumber writes (assumed 100:1).
- Degrade, don't fail, when Redis is unavailable. Postgres is the source of truth; Redis is only an accelerator.
- Everything is observable: JSON logs with request ids, and model decisions logged with the model version.
- Config is entirely environment-driven, there are no secrets in code, and all SQL is parameterised.

**Out of scope (deliberately):** user accounts and a UI, custom domains, editing a link's destination (a security risk: a vetted link could be re-pointed after sharing), and link previews.

## 2. Capacity estimate

**Assumed load:** 10 M new links/month and 100 redirects per link-month.

| Quantity | Math | Result |
|---|---|---|
| Write QPS (avg) | 10 M / (30 × 86,400 s) | **~3.9/s** |
| Write QPS (peak, 10×) | | **~40/s** |
| Redirect QPS (avg) | 1 B / 2.59 M s | **~390/s** |
| Redirect QPS (peak, 10×) | | **~4,000/s** |
| Code space | 62^7 | 3.5 × 10^12 codes, about 29,000 years at 120 M links/year |

**Storage per row** (rough: Postgres tuple header ~24 B, plus columns, plus index entries):

| Table | Row + indexes | Volume | Growth |
|---|---|---|---|
| `links` | ~300 B (100 B average URL; pk, `code` unique, `owner_key_id` indexes) | 10 M/month | **~3 GB/month** |
| `screening_events` | ~200 B | one per create | ~2 GB/month |
| `clicks` | ~210 B (row ~120 B; pk, `event_id` unique, `(link_id, clicked_at)`) | 1 B/month | **~210 GB/month** |

Clicks dominate storage by two orders of magnitude, which shapes the scaling plan (§8).

**Redis**

- Cache entries are about 250 B each. A hot set of 1 M links within one TTL window is about 250 MB.
- The click stream holds about 300 B per event. At the peak of 4,000 clicks/s, the default `CLICK_STREAM_MAX_LENGTH=500000` buffers only about 2 minutes of worker downtime before the oldest events are trimmed. A production setting should size it for the longest worker outage you want to absorb, for example 10 minutes ≈ 2.4 M entries ≈ 720 MB.

**Database load at peak**

- With a 95% cache hit rate, redirect misses cost about 200 primary-key lookups/s.
- Click ingestion costs 4,000 rows/s, which at 500 rows per batch is only 8 INSERT statements/s.
- Neither is a challenge for a single Postgres primary.

**API**

- **Measured:** two uvicorn worker processes served ~690 redirects/s while CPU-bound on a shared 4 vCPU box. That is roughly 350/s per process with the load generator on the same host.
- The 4,000/s peak therefore needs about 12 processes, say 16 with headroom (four 4-vCPU instances). That figure should be re-measured on production hardware before it is relied on.

## 3. Data model

```
api_keys (id, name, prefix, key_hash UNIQUE, created_at, revoked_at)
   │ 1
   │ n
links (id, code UNIQUE COLLATE "C", long_url, owner_key_id, is_custom, status,
       expires_at, screening_decision, screening_score, model_version, created_at, updated_at)
   │ 1                                  │ 1
   │ n                                  │ 0..1
clicks (id, event_id UNIQUE, link_id,   screening_events (id, url, score, decision,
        clicked_at, referrer_host,          model_version, api_key_id, link_id, created_at)
        browser, os, device_type)
```

### `links`: what each column is for

| Column | Type | Why |
|---|---|---|
| `id` | `BIGINT`, from `links_id_seq` | Source of the collision-free code (§4). 8 bytes, because a 4-byte int runs out at 2.1 B. |
| `code` | `VARCHAR(32) COLLATE "C"`, `UNIQUE` | The redirect key. `"C"` collation makes comparison byte-wise and case-sensitive (base62 needs both cases) and is the cheapest comparison for the btree. The unique constraint **is** the lookup index (no second index) and is the last-line guarantee against duplicates. 32 chars fits aliases; generated codes are 7. |
| `long_url` | `TEXT`, `CHECK (char_length ≤ 2048)` | URLs regularly exceed 255. The cap matches what browsers and most CDNs handle reliably and bounds row size. Not indexed: nothing looks links up by destination (see the dedup decision below). |
| `owner_key_id` | `BIGINT` FK → `api_keys`, indexed | Ownership checks on every management call, and "list my links". `ON DELETE RESTRICT`: keys are revoked, not deleted. |
| `is_custom` | `BOOLEAN` | Distinguishes aliases from generated codes for analytics and support. The collision retry logic does not depend on it. |
| `status` | enum `active / disabled` | Soft delete. A disabled code is **never reused**: reuse would silently re-point every old copy of the link (emails, QR codes) to a new destination. |
| `expires_at` | `TIMESTAMPTZ NULL` | Evaluated at read time, so no cron job flips rows to "expired". `TIMESTAMPTZ` always, so time zones are never ambiguous. |
| `screening_decision`, `screening_score`, `model_version` | enum / real / text, nullable | Denormalised from `screening_events` so link reads need no join. NULL means the link predates screening. A `CHECK` forbids `block`, since blocked URLs never become links. |
| `created_at`, `updated_at` | `TIMESTAMPTZ` | Audit trail. |

**Indexes not added, and why:**

- No index on `long_url`. Each create gets its own code, even for the same URL, so every owner gets separate analytics and expiry, and lookup-by-URL is never needed.
- No index on `expires_at`. Expiry is checked per row at read time, and nothing scans for expired rows.

If dedup or expiry cleanup were added later, those indexes would come with them.

### `clicks`

- `(link_id, clicked_at)` composite index: every stats query is "clicks of link X since time T", which this index answers with a range scan.
- `event_id UNIQUE`: the Redis Stream delivers at least once. Inserting with `ON CONFLICT (event_id) DO NOTHING` makes redelivery harmless, so the pipeline counts each click exactly once (tested).
- Only the referrer **host** and **no IP address** are stored. That is enough for "top referrers" and avoids keeping personal data that full referrer URLs and IPs carry. User agents are parsed into browser, OS and device in the worker, not on the redirect path.

### `api_keys`

- Keys are 32 random bytes and are stored as `HMAC-SHA256(pepper, key)`.
  - A fast hash is correct for high-entropy tokens. bcrypt and argon2 exist to slow down guessing of *low-entropy* passwords; on random keys they would only add about 100 ms per request.
  - The pepper means a stolen database dump alone cannot be used to test candidate keys.
- `key_hash UNIQUE` makes authentication a single index lookup.
- `prefix` (the first 12 characters) identifies a key in logs and in the CLI without revealing it.

### `screening_events`

- One row per classifier decision, **including blocks**, which never create a link. This is the data that false-positive review, drift monitoring and retraining need (see ML.md).
- Partial index on `created_at WHERE decision IN ('review','block')`: the review queue reads only flagged rows, and the index stays tiny because most decisions are `allow`.

### Migrations

There are four additive migrations, one per feature slice. Two techniques are worth knowing:

- **0003 expand → backfill → contract.** It adds `owner_key_id` as nullable, assigns any existing links to a revoked placeholder key, then sets `NOT NULL`. Adding a `NOT NULL` column to a populated table in one step would fail.
- **Migrations run as a one-shot job** (the `migrate` compose service), never at application startup. Several replicas starting together would race to migrate.

CI runs `alembic check` to prove the models and migrations agree, and the test suite migrates down to base and back up on every run.

## 4. Short-code generation

**Requirement:** two different links must never get the same code, without trusting luck.

### Chosen: counter + keyed permutation + base62

```
id   = nextval('links_id_seq')               -- unique by construction, no race
code = base62( feistel_permute(id, CODE_SECRET), width=7 )
```

**Why it cannot collide**

- A Feistel network is a permutation, whatever its round function. Cycle-walking restricts it to `[0, 62^7)` and keeps it a bijection.
- A bijection maps distinct ids to distinct codes, and the sequence hands out each id exactly once, even across API instances and rolled-back transactions.
- So collision-freedom is a property of the math, not of a retry loop. The tests check bijectivity exhaustively on a 3,844-code space and on 100 k production-length ids.

**Why permute at all?** Plain `base62(id)` gives `0000001`, `0000002`, and so on. That leaks business volume and lets anyone enumerate every link by counting. The keyed permutation makes neighbouring ids produce unrelated codes.

**The one remaining overlap: custom aliases.**

- An alias is just a code someone chose. If someone took the alias `Ab3xY9q` and the permutation later maps some id to exactly that string, the insert hits `UNIQUE(code)`.
- The service catches that specific constraint violation, inside a savepoint so the transaction survives, takes the next id, and retries, up to 5 times.
- This branch is tested. In practice it needs an alias that is exactly 7 base62 characters *and* equal to a future code.
- An alias that collides with an existing code is a plain **409**.

**Caveat:** `CODE_SECRET` must never rotate. A new key re-maps future ids onto strings that existing links may already use. The unique constraint and retry would still prevent duplicates, but collisions would become routine rather than rare.

### Alternatives considered

| Approach | How it handles collisions | Costs |
|---|---|---|
| **Random code + retry** | Insert a random 7-char code; on unique violation, try again. The chance of a clash per insert is n / 62^7: about 0.03% at 1 B links. | No coordination point, but every insert needs a retry path and the probability grows with the table. Non-deterministic latency, and still needs the unique index. Perfectly viable; the counter is simply provable. |
| **Hash of the URL, truncated** | Deterministic: the same URL gives the same code. | Truncation to 7 chars hits the birthday bound: a 50% chance of *some* collision after about 1.18 × √(62^7) ≈ **2.2 M links**. Collisions must then be resolved anyway (salt and retry). It also forces dedup, so per-owner analytics and expiry are lost. |
| **Plain counter + base62** | No collisions. | Sequential and enumerable (see above). |
| **Pre-generated key pool** (a "KGS" table of unused codes) | Hand out pre-made codes. | An extra service and table to keep stocked. Useful mostly when there is no single sequence to rely on. |

**Scaling the counter:**

- One Postgres sequence handles tens of thousands of `nextval` calls per second, far beyond the 40/s peak.
- With several write regions, you would allocate id ranges per region (hi/lo) or reserve high bits of the id for the region. The permutation keeps working, because it only needs unique ids.

**Codes are not secrets.** Anyone holding a link can follow it. Private destinations need authentication at the destination, not obscurity in the code.

## 5. Caching strategy

**Cache-aside on the redirect path** (`app/services/cache.py`):

1. `GET link:{code}` from Redis. On a hit, redirect without touching Postgres; the request never even checks out a DB connection.
2. On a miss, run a primary-key-style lookup on `links.code`, then `SET link:{code} … EX 600`.
3. On writes (PATCH, DELETE, and create, to clear a cached 404), commit to Postgres **first**, then `DEL` the key.

**Design points**

- **What is cached:** `{id, url, status, expires_at}`. The id lets the click event be built without a DB read.
- **Expiry is checked on every read.** A cached link that expires mid-TTL still returns 410 on time; the TTL governs memory and staleness, not expiry correctness.
- **Negative caching:** unknown codes are cached as `absent` for 30 s. Bots scanning random codes would otherwise turn every guess into a Postgres query. Creating an alias deletes its key, so a code probed before it existed works immediately.
- **Why delete instead of update on write:** two concurrent writers that each `SET` their value can leave the older one in cache. Deleting cannot. The next read repopulates the cache from the committed row.
- **Known race:**
  - Reader A misses and reads the old row. Writer B commits and deletes the key. Reader A then writes the old value back.
  - The stale entry lives until the TTL expires. That is why the TTL is a modest 10 minutes rather than days. For a shortener, a disable taking effect up to 10 minutes late in this rare interleaving is acceptable.
  - Stricter options cost more: versioned keys, or "delete, wait, delete again".
- **Stampede:** if a hot key expires, many concurrent requests miss at once. Here each miss is one indexed lookup, so a burst is cheap. At much higher scale, add single-flight (one request repopulates the key while the others wait or serve stale) or probabilistic early refresh.
- **Why not an in-process cache?** It would be faster, but with several instances, invalidating every process's copy needs pub/sub. A small in-process L1 with a few seconds of TTL in front of Redis is a reasonable next step for very hot links.
- **Redis down:** reads behave as misses, and writes and invalidations are skipped and logged.
  - A circuit breaker (`app/core/redis.py`) skips Redis for 5 s after a failure. Without it, every request would first wait out a connection timeout.
  - An invalidation lost during an outage is bounded by the TTL.

**Why 302 and `Cache-Control: no-store`:** a 301 is cached permanently by browsers. Clicks would then stop reaching us (no analytics), and disabling a link would not affect anyone who had already followed it. The cost is that every click reaches the origin, and CDNs cannot cache redirects. Edge compute that redirects and logs at the edge is the scale answer (§8).

## 6. Async analytics

```
redirect handler --(after response is sent)--> XADD clicks:stream
worker: XAUTOCLAIM (stale) + XREADGROUP (new) -> parse UA -> INSERT ... ON CONFLICT DO NOTHING
        -> COMMIT -> XACK + XDEL
```

- **Off the hot path:** the event is enqueued in a Starlette background task, which runs after the 302 has been sent.
- **Durable and decoupled:** events survive API restarts and deploys, unlike in-process tasks. The worker scales independently, and a consumer group gives each event to exactly one worker.
- **At-least-once + idempotent sink = effectively-once:**
  - The worker acknowledges only after the database commit, so a crash between the two means redelivery.
  - The unique `event_id` (the stream entry id) turns the redelivered insert into a no-op.
  - Events held by a dead worker are re-claimed after `WORKER_CLAIM_IDLE_MS`.
- **Poison messages** (malformed events) are logged and dropped rather than blocking the queue forever.
- **Redis down:** the click is inserted directly from the API process, one row per click. That costs more database load, but no data is lost.
- **Worker down:** events accumulate in the stream, up to `CLICK_STREAM_MAX_LENGTH` (see §2 for sizing).
- **Batching:** up to 500 events per INSERT, which matters for both Postgres write amplification and round trips.

## 7. Failure modes

| Failure | Effect | Handling |
|---|---|---|
| Redis down | Cache, rate limiter and click queue unavailable | Circuit breaker fails fast. Redirects read Postgres; rate limiting **fails open** (logged), because rejecting every create during a cache outage is worse; clicks are written directly. `/readyz` reports `degraded` but stays 200, since failing readiness on a shared Redis would pull *every* instance out of the load balancer at once. |
| Postgres down | No source of truth | Cached redirects keep working until TTL. Everything else returns 500 with the error envelope. `/readyz` returns 503 so the load balancer drains the instance; `/healthz` stays 200 so the orchestrator does not restart-loop a healthy process. |
| Worker down or slow | Stats lag | Events queue in the stream and are re-claimed on restart. Stats are eventually consistent. |
| Model artifact missing or tampered | Cannot screen | The app **refuses to start**: the artifact's sha256 must match its metadata before it is unpickled. Better a failed deploy than silently unscreened links. |
| Model slow | Create latency | Inference runs in a thread pool, so redirects are unaffected. **Measured:** p50 5 ms, p99 20 ms per URL. |
| Burst of creates from one key or IP | Load, abuse | Sliding-window limits per key and per IP return 429 with `Retry-After`. Rejected requests are not counted, so a retrying client gets back in as its window slides. |
| Duplicate submissions | Two links for the same URL | By design (§3). Clients that need idempotency should use an alias. |
| Clock skew between instances | Inconsistent rate limits | The limiter uses Redis `TIME`, not instance clocks. |
| Redis restarts without data | Consumer group lost | The worker recreates the group after any error. AOF persistence (`--appendonly yes`) is enabled in compose. |

## 8. Horizontal scaling

The API is stateless: config comes from the environment, and state lives in Postgres and Redis. Scaling it means adding replicas behind a load balancer. Below is the order in which bottlenecks would appear, and what to do about each.

1. **API CPU.** This is the first limit (**measured**: CPU-bound at about 350 redirects/s per process on the test box). Add uvicorn workers and replicas.
   - Each process holds a database pool (`DB_POOL_SIZE` + overflow). Past a few dozen processes, put **PgBouncer** in transaction mode in front of Postgres so connection count stays bounded.
2. **Clicks table growth (~210 GB/month at the assumed load).**
   - Partition `clicks` by month. Postgres requires the partition key in unique constraints, so `event_id` becomes `UNIQUE (event_id, clicked_at)`.
   - Keep raw rows for a retention window, for example 90 days, and roll older data up into a `daily_link_stats (link_id, day, clicks, by_device…)` table that the stats endpoint reads. That turns `COUNT(*)` over millions of rows into a few small-row reads.
   - At larger scale, move raw clicks to a columnar store (ClickHouse, BigQuery). The worker is the only writer, so this swap is local.
3. **Click workers.** Add replicas: the consumer group partitions work automatically. For more throughput than one stream can carry, shard streams by `link_id % N`.
4. **Postgres reads.** Redirect misses and stats can go to read replicas. Replica lag only affects freshness that is already eventual. Writes (about 40/s at peak) stay on the primary for a long time.
5. **Redis.** Use a managed service or Sentinel for failover first, then Redis Cluster for capacity. One catch: the rate-limit Lua script touches two keys (`key:` and `ip:`), which in a cluster must hash to the same slot. Either run the script once per subject, accepting that the all-or-nothing property is lost, or use hash tags.
6. **Global latency.** Move redirects to the edge, for example a CDN worker with a replicated key-value copy of `code → url`, logging clicks asynchronously back to the pipeline. The origin then serves only management traffic and edge misses.
7. **Model serving.** Inference is a few milliseconds in-process. If the model grows (embeddings, a transformer), move it to a separate model server with its own scaling and batching. The service already calls the scorer through one interface (`UrlScorer`), so only `build_screener` changes.
