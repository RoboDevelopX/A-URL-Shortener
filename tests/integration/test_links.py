import asyncio
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy import update

from app.models.link import Link

LINKS = "/api/v1/links"


async def create(client: AsyncClient, **body: object) -> dict[str, object]:
    response = await client.post(LINKS, json={"url": "https://example.com/page", **body})
    assert response.status_code == 201, response.text
    data: dict[str, object] = response.json()
    return data


async def expire_in_db(app: FastAPI, code: str) -> None:
    """The API refuses past expiries, so tests move expiry into the past directly."""
    async with app.state.session_factory() as session:
        await session.execute(
            update(Link)
            .where(Link.code == code)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()


async def test_create_returns_generated_code_and_location(client: AsyncClient) -> None:
    response = await client.post(LINKS, json={"url": "https://example.com/a?b=c"})
    assert response.status_code == 201
    body = response.json()
    assert len(body["code"]) == 7
    assert body["short_url"] == f"http://localhost:8000/{body['code']}"
    assert body["long_url"] == "https://example.com/a?b=c"
    assert body["status"] == "active"
    assert body["is_custom"] is False
    assert response.headers["location"] == f"/api/v1/links/{body['code']}"


async def test_redirect_returns_302_to_destination(client: AsyncClient) -> None:
    link = await create(client)
    response = await client.get(f"/{link['code']}")
    assert response.status_code == 302
    assert response.headers["location"] == "https://example.com/page"
    assert response.headers["cache-control"] == "no-store"


async def test_unknown_and_malformed_codes_are_404(client: AsyncClient) -> None:
    assert (await client.get("/nope123")).status_code == 404
    response = await client.get("/not.a.code")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_custom_alias(client: AsyncClient) -> None:
    link = await create(client, alias="spring-sale")
    assert link["code"] == "spring-sale"
    assert link["is_custom"] is True
    assert (await client.get("/spring-sale")).status_code == 302


async def test_alias_conflict_is_409(client: AsyncClient) -> None:
    await create(client, alias="taken")
    response = await client.post(LINKS, json={"url": "https://other.org", "alias": "taken"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "alias_conflict"


async def test_aliases_are_case_sensitive(client: AsyncClient) -> None:
    await create(client, alias="Promo")
    await create(client, alias="promo")


async def test_reserved_alias_is_rejected(client: AsyncClient) -> None:
    response = await client.post(LINKS, json={"url": "https://example.com", "alias": "admin"})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_alias"


async def test_generated_code_that_collides_with_an_alias_is_retried(
    app: FastAPI, client: AsyncClient
) -> None:
    # The alias row takes id 1; the next generated link would get id 2, whose code we
    # claim in advance as an alias. The service must notice and move on to id 3.
    codes = app.state.codes
    squatted = codes.encode(2)
    await create(client, alias=squatted)

    generated = await create(client)
    assert generated["code"] == codes.encode(3)


async def test_concurrent_creates_get_distinct_codes(client: AsyncClient) -> None:
    responses = await asyncio.gather(
        *(client.post(LINKS, json={"url": f"https://example.com/{i}"}) for i in range(40))
    )
    assert all(r.status_code == 201 for r in responses)
    assert len({r.json()["code"] for r in responses}) == 40


async def test_invalid_url_is_422(client: AsyncClient) -> None:
    for url in ["http://localhost/x", "http://192.168.0.1", "ftp://example.com", "nonsense"]:
        response = await client.post(LINKS, json={"url": url})
        assert response.status_code == 422, url
        assert response.json()["error"]["code"] == "invalid_url"


async def test_schema_validation_errors_use_the_error_envelope(client: AsyncClient) -> None:
    response = await client.post(LINKS, json={"url": "https://example.com", "extra": 1})
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "validation_error"
    assert body["details"][0]["loc"] == ["body", "extra"]


async def test_expiry_must_be_in_the_future(client: AsyncClient) -> None:
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    response = await client.post(LINKS, json={"url": "https://example.com", "expires_at": past})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_expiry"


async def test_expiry_requires_a_timezone(client: AsyncClient) -> None:
    response = await client.post(
        LINKS, json={"url": "https://example.com", "expires_at": "2099-01-01T00:00:00"}
    )
    assert response.status_code == 422


async def test_expired_link_returns_410(app: FastAPI, client: AsyncClient) -> None:
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    link = await create(client, expires_at=future)
    assert (await client.get(f"/{link['code']}")).status_code == 302

    await expire_in_db(app, str(link["code"]))
    response = await client.get(f"/{link['code']}")
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "link_gone"

    meta = await client.get(f"{LINKS}/{link['code']}")
    assert meta.json()["status"] == "expired"


async def test_get_metadata(client: AsyncClient) -> None:
    link = await create(client, alias="meta-test")
    response = await client.get(f"{LINKS}/meta-test")
    assert response.status_code == 200
    assert response.json()["long_url"] == link["long_url"]
    assert (await client.get(f"{LINKS}/missing")).status_code == 404


async def test_patch_updates_and_clears_expiry(client: AsyncClient) -> None:
    link = await create(client)
    future = datetime.now(UTC) + timedelta(days=7)
    response = await client.patch(
        f"{LINKS}/{link['code']}", json={"expires_at": future.isoformat()}
    )
    assert response.status_code == 200
    assert datetime.fromisoformat(response.json()["expires_at"]) == future

    cleared = await client.patch(f"{LINKS}/{link['code']}", json={"expires_at": None})
    assert cleared.json()["expires_at"] is None


async def test_patch_can_revive_an_expired_link(app: FastAPI, client: AsyncClient) -> None:
    link = await create(client)
    await expire_in_db(app, str(link["code"]))
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    await client.patch(f"{LINKS}/{link['code']}", json={"expires_at": future})
    assert (await client.get(f"/{link['code']}")).status_code == 302


async def test_patch_rejects_past_expiry_and_missing_field(client: AsyncClient) -> None:
    link = await create(client)
    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    assert (
        await client.patch(f"{LINKS}/{link['code']}", json={"expires_at": past})
    ).status_code == 422
    assert (await client.patch(f"{LINKS}/{link['code']}", json={})).status_code == 422


async def test_delete_disables_link(client: AsyncClient) -> None:
    link = await create(client)
    assert (await client.delete(f"{LINKS}/{link['code']}")).status_code == 204
    assert (await client.get(f"/{link['code']}")).status_code == 410
    assert (await client.get(f"{LINKS}/{link['code']}")).json()["status"] == "disabled"
    # Idempotent, and a disabled link cannot be edited back to life.
    assert (await client.delete(f"{LINKS}/{link['code']}")).status_code == 204
    patch = await client.patch(f"{LINKS}/{link['code']}", json={"expires_at": None})
    assert patch.status_code == 410


async def test_disabled_code_is_never_reused_as_alias(client: AsyncClient) -> None:
    await create(client, alias="gone-soon")
    await client.delete(f"{LINKS}/gone-soon")
    response = await client.post(
        LINKS, json={"url": "https://evil.example.org", "alias": "gone-soon"}
    )
    assert response.status_code == 409
