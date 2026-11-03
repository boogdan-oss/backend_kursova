from httpx import AsyncClient


async def test_ping(client: AsyncClient) -> None:
    response = await client.get("/api/v1/ping")
    assert response.status_code == 200
    assert response.json()["message"] == "pong"


async def test_health_reports_components(client: AsyncClient) -> None:
    response = await client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["components"]["api"]["status"] == "ok"
    assert {"db", "redis"} <= body["components"].keys()
    assert body["status"] in {"ok", "degraded"}
