import asyncio

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import select

from app.cli import main as cli_main
from app.core.security import hash_api_key
from app.models.api_key import ApiKey
from tests.integration.helpers import issue_key

LINKS = "/api/v1/links"

MANAGEMENT_CALLS = [
    ("POST", LINKS, {"url": "https://example.com"}),
    ("GET", f"{LINKS}/abc1234", None),
    ("PATCH", f"{LINKS}/abc1234", {"expires_at": None}),
    ("DELETE", f"{LINKS}/abc1234", None),
    ("GET", f"{LINKS}/abc1234/stats", None),
]


@pytest.mark.parametrize(("method", "path", "body"), MANAGEMENT_CALLS)
async def test_management_endpoints_require_a_key(
    anon_client: AsyncClient, method: str, path: str, body: dict[str, object] | None
) -> None:
    response = await anon_client.request(method, path, json=body)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["www-authenticate"] == 'ApiKey header="X-API-Key"'


@pytest.mark.parametrize(("method", "path", "body"), MANAGEMENT_CALLS)
async def test_wrong_key_is_rejected(
    anon_client: AsyncClient, method: str, path: str, body: dict[str, object] | None
) -> None:
    response = await anon_client.request(
        method, path, json=body, headers={"X-API-Key": "usk_not-a-real-key"}
    )
    assert response.status_code == 401


async def test_redirects_are_public(client: AsyncClient, anon_client: AsyncClient) -> None:
    await client.post(LINKS, json={"url": "https://example.com", "alias": "public"})
    assert (await anon_client.get("/public")).status_code == 302


async def test_keys_are_stored_hashed(app: FastAPI, api_key: str) -> None:
    async with app.state.session_factory() as session:
        rows = (await session.execute(select(ApiKey))).scalars().all()
    assert len(rows) == 1
    assert rows[0].key_hash != api_key
    assert api_key not in {rows[0].key_hash, rows[0].prefix}
    pepper = app.state.settings.api_key_pepper.get_secret_value().encode()
    assert rows[0].key_hash == hash_api_key(api_key, pepper)
    assert api_key.startswith(rows[0].prefix)


async def test_links_are_scoped_to_their_owner(
    app: FastAPI, client: AsyncClient, anon_client: AsyncClient
) -> None:
    await client.post(LINKS, json={"url": "https://example.com", "alias": "mine"})
    other = {"X-API-Key": await issue_key(app, "other")}
    for method, path, body in [
        ("GET", f"{LINKS}/mine", None),
        ("PATCH", f"{LINKS}/mine", {"expires_at": None}),
        ("DELETE", f"{LINKS}/mine", None),
        ("GET", f"{LINKS}/mine/stats", None),
    ]:
        response = await anon_client.request(method, path, json=body, headers=other)
        assert response.status_code == 404, (method, path)
    assert (await client.get(f"{LINKS}/mine")).status_code == 200
    assert (await anon_client.get("/mine")).status_code == 302  # still active


async def test_revoked_key_is_rejected_via_cli(app: FastAPI, client: AsyncClient) -> None:
    assert (await client.get(f"{LINKS}/x/stats")).status_code == 404  # key works
    prefix = str(client.headers["X-API-Key"])[:12]
    # The CLI runs its own event loop, so call it from a worker thread.
    code = await asyncio.to_thread(
        cli_main, ["revoke-api-key", "--prefix", prefix], app.state.settings
    )
    assert code == 0
    assert (await client.get(f"{LINKS}/x/stats")).status_code == 401


async def test_cli_issues_working_keys(
    app: FastAPI, anon_client: AsyncClient, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        await asyncio.to_thread(
            cli_main, ["create-api-key", "--name", "cli", "--quiet"], app.state.settings
        )
        == 0
    )
    key = capsys.readouterr().out.strip()
    response = await anon_client.post(
        LINKS, json={"url": "https://example.com"}, headers={"X-API-Key": key}
    )
    assert response.status_code == 201

    assert (
        await asyncio.to_thread(
            cli_main, ["create-api-key", "--name", "verbose"], app.state.settings
        )
        == 0
    )
    assert "cannot be shown again" in capsys.readouterr().out
    assert (
        await asyncio.to_thread(
            cli_main, ["revoke-api-key", "--prefix", "usk_nomatch0"], app.state.settings
        )
        == 1
    )
