from httpx import ASGITransport, AsyncClient

from app.main import create_app
from tests.conftest import make_settings


async def test_healthz_is_ok(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_reports_dependencies(client: AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["checks"] == {"database": "ok", "redis": "ok"}


async def test_readyz_is_degraded_but_ready_when_redis_is_down() -> None:
    app = create_app(make_settings(redis_url="redis://127.0.0.1:1/0"))
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {
        "status": "degraded",
        "checks": {"database": "ok", "redis": "unavailable"},
    }


async def test_readyz_is_503_when_database_is_down() -> None:
    app = create_app(
        make_settings(database_url="postgresql+asyncpg://nobody:wrong@127.0.0.1:1/none")
    )
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client,
    ):
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["checks"]["database"] == "unavailable"


async def test_request_id_is_generated_and_echoed(client: AsyncClient) -> None:
    generated = await client.get("/healthz")
    assert len(generated.headers["x-request-id"]) == 32

    echoed = await client.get("/healthz", headers={"X-Request-ID": "abc-123"})
    assert echoed.headers["x-request-id"] == "abc-123"


async def test_unsafe_request_id_is_replaced(client: AsyncClient) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "bad id\nINJECTED"})
    assert response.headers["x-request-id"] != "bad id\nINJECTED"


async def test_unknown_route_uses_error_schema(client: AsyncClient) -> None:
    response = await client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["code"] == "not_found"
    assert body["error"]["request_id"] == response.headers["x-request-id"]
