import asyncio

import httpx

from app.auth import RequestAuth
from app.gitea import GiteaClient, use_http_client


def test_gitea_clients_reuse_lifespan_http_pool() -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    async def run() -> None:
        transport = httpx.MockTransport(handle)
        async with httpx.AsyncClient(
            base_url="http://gitea:3000/api/v1",
            transport=transport,
        ) as client:
            async with use_http_client(client):
                first = GiteaClient(RequestAuth("token first"))
                second = GiteaClient(RequestAuth("token second"))
                assert await first.request("GET", "/first") == {"ok": True}
                assert await second.request("GET", "/second") == {"ok": True}

    asyncio.run(run())
    assert [request.url.path for request in requests] == [
        "/api/v1/first",
        "/api/v1/second",
    ]
    assert [request.headers["authorization"] for request in requests] == [
        "token first",
        "token second",
    ]
