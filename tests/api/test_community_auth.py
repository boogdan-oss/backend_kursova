import pytest
from httpx import AsyncClient


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/community/posts"),
        ("PATCH", "/api/v1/community/posts/1"),
        ("DELETE", "/api/v1/community/posts/1"),
        ("PUT", "/api/v1/community/posts/1/like"),
        ("PUT", "/api/v1/community/posts/1/repost"),
        ("POST", "/api/v1/community/posts/1/comments"),
        ("DELETE", "/api/v1/community/comments/1"),
        ("POST", "/api/v1/community/reports"),
        ("GET", "/api/v1/admin/stats"),
        ("GET", "/api/v1/admin/reports"),
        ("POST", "/api/v1/admin/bans"),
        ("DELETE", "/api/v1/admin/bans/1"),
    ],
)
async def test_writes_and_moderation_need_login(
    client: AsyncClient, method: str, path: str
) -> None:
    response = await client.request(method, path, json={})

    assert response.status_code == 401
    assert response.json()["detail"] == "Потрібно увійти"
