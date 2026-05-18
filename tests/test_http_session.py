"""Outbound aiohttp session wiring tests."""

from __future__ import annotations

import asyncio
import importlib

import aiohttp
from aiohttp import web


def test_http_session_uses_explicit_tcp_connector(monkeypatch) -> None:
    monkeypatch.setenv("SPIDER_HTTP_KEEPALIVE_TIMEOUT_S", "12.5")
    monkeypatch.setenv("SPIDER_HTTP_CONN_LIMIT", "30")
    monkeypatch.setenv("SPIDER_HTTP_CONN_LIMIT_PER_HOST", "7")
    monkeypatch.setenv("SPIDER_HTTP_DNS_CACHE_TTL_S", "45")
    monkeypatch.setenv("SPIDER_HTTP_ENABLE_CLEANUP_CLOSED", "0")

    import spider_server

    spider_server = importlib.reload(spider_server)
    async def _go() -> None:
        sess = spider_server._build_http_session()
        try:
            conn = sess.connector
            assert isinstance(conn, aiohttp.TCPConnector)
            assert conn.force_close is False
            assert conn.limit == 30
            assert conn.limit_per_host == 7
            assert conn._keepalive_timeout == 12.5  # noqa: SLF001
        finally:
            await sess.close()

    asyncio.run(_go())


def test_http_session_reuses_connections() -> None:
    async def _go() -> tuple[int, int]:
        app = web.Application()

        async def _ok(_request: web.Request) -> web.Response:
            return web.json_response({"ok": True})

        app.router.add_get("/ok", _ok)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        sock = site._server.sockets[0]  # type: ignore[union-attr]
        port = sock.getsockname()[1]

        stats = {"created": 0, "reused": 0}
        trace = aiohttp.TraceConfig()

        async def _on_conn_create_start(
            _session: aiohttp.ClientSession,
            _trace_ctx: object,
            _params: object,
        ) -> None:
            stats["created"] += 1

        async def _on_conn_reuse(
            _session: aiohttp.ClientSession,
            _trace_ctx: object,
            _params: object,
        ) -> None:
            stats["reused"] += 1

        trace.on_connection_create_start.append(_on_conn_create_start)
        trace.on_connection_reuseconn.append(_on_conn_reuse)

        import spider_server

        sess = spider_server._build_http_session(trace_configs=[trace])
        try:
            for _ in range(3):
                async with sess.get(f"http://127.0.0.1:{port}/ok") as resp:
                    assert resp.status == 200
                    await resp.read()
        finally:
            await sess.close()
            await runner.cleanup()

        return stats["created"], stats["reused"]

    created, reused = asyncio.run(_go())
    assert created == 1
    assert reused >= 2
