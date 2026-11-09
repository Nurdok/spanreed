import asyncio

from spanreed.plugins.web_ui import build_app


def test_root_redirects_and_old_log_endpoints_are_gone() -> None:
    async def run() -> None:
        client = build_app().test_client()
        root = await client.get("/")
        assert root.status_code == 302
        assert root.headers["Location"] == "/app/"
        # The unauthenticated log WebSocket no longer exists.
        assert (await client.get("/logs-ws")).status_code == 404
        health = await client.get("/healthz")
        assert health.status_code == 200
        assert await health.get_data(as_text=True) == "ok"

    asyncio.run(run())
