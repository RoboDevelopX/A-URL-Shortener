"""Load test: mostly redirects (the hot path), some stats reads and creates.

    make loadtest                       # 50 users for 60 s against the compose stack
    LOADTEST_API_KEY=... uv run --group loadtest locust -f loadtest/locustfile.py \\
        --host http://localhost:8000    # interactive UI on :8089

The traffic mix mirrors a shortener's real shape: reads dominate writes by orders of
magnitude. Creates are rate limited per key and per IP, and every simulated user shares
one key and one IP, so most creates are *expected* to get 429; those count as successes
here because the limiter answering quickly is the behaviour under test.
"""

import json
import os
import random
import urllib.request
from typing import Any

from locust import FastHttpUser, events, task
from locust.env import Environment

SEED_LINKS = 20
CODES: list[str] = []


def _headers() -> dict[str, str]:
    return {"X-API-Key": os.environ["LOADTEST_API_KEY"], "Content-Type": "application/json"}


@events.test_start.add_listener
def seed_links(environment: Environment, **_: Any) -> None:
    """Create the links that redirect traffic will hit, before users start."""
    host = environment.host or "http://localhost:8000"
    for i in range(SEED_LINKS):
        request = urllib.request.Request(  # noqa: S310  # host is the configured target
            f"{host}/api/v1/links",
            data=f'{{"url": "https://example.com/load/{i}"}}'.encode(),
            headers=_headers(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310
            CODES.append(json.loads(response.read())["code"])


class ShortenerUser(FastHttpUser):
    @task(40)
    def follow_link(self) -> None:
        with self.client.get(
            f"/{random.choice(CODES)}",  # noqa: S311  # not cryptographic
            name="GET /{code}",
            allow_redirects=False,
            catch_response=True,
        ) as response:
            if response.status_code == 302:
                response.success()
            else:
                response.failure(f"expected 302, got {response.status_code}")

    @task(2)
    def read_stats(self) -> None:
        self.client.get(
            f"/api/v1/links/{random.choice(CODES)}/stats",  # noqa: S311
            name="GET /api/v1/links/{code}/stats",
            headers=_headers(),
        )

    @task(1)
    def create_link(self) -> None:
        with self.client.post(
            "/api/v1/links",
            json={"url": f"https://example.com/new/{random.randint(0, 10**9)}"},  # noqa: S311
            headers=_headers(),
            name="POST /api/v1/links",
            catch_response=True,
        ) as response:
            if response.status_code in (201, 429):
                response.success()
            else:
                response.failure(f"unexpected {response.status_code}")
