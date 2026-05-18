"""aiohttp HTTP server — rag-stack-compatible :11700 surface.

Runs the full v1 surface:

  GET    /health
  POST   /fetch            enqueue
  POST   /fetch/sync       inline fetch
  POST   /feeds            register
  GET    /feeds            list
  GET    /feeds/{id}       get one
  DELETE /feeds/{id}       remove
  POST   /feeds/{id}/poll  force-poll
  GET    /jobs?limit=50    recent

On startup we connect to Redis (via ``REDIS_URL``) and spawn two
background tasks:

  - **dispatcher** drains ``crawler:queue`` and fan-outs to bounded
    workers.
  - **poller** wakes when the next-due feed becomes due and re-polls it.

Both are cancel-safe and unwind cleanly when ``_on_cleanup`` fires.

This module is the production entry point. Tests construct their own
app via ``spider.routes.register_routes`` so they don't run the
startup hook.
"""
from __future__ import annotations

import asyncio
import logging
import os

import aiohttp
import redis.asyncio as redis_asyncio
from aiohttp import web

from spider.browser import BROWSER_MAX_PAGES, CloakBrowserPool
from spider.browsers import env_backend, make_pool, register_primary, shutdown_all
from spider.feeds import poller
from spider.fetch import FetchDeps
from spider.politeness import HostBucket, RobotsCache
from spider.queue import dispatcher
from spider.routes import register_routes
from spider.sessions import SessionManager

logger = logging.getLogger("spider-http")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


async def _on_startup(app: web.Application) -> None:
    """Build long-lived deps + spawn background tasks.

    CloakBrowser is opened lazily on the first lease (``_open`` only
    runs inside ``CloakBrowserPool.lease()``), so the Chromium binary
    isn't fetched until the first ``/fetch/sync`` arrives.
    """
    http = aiohttp.ClientSession()
    # Choose the primary browser backend from env. Defaults to cloak.
    # The pool's _open() is lazy — Chromium / CDP isn't initialised
    # until the first lease, so this is cheap even if we never fetch.
    backend = env_backend()
    pool = make_pool(backend, max_pages=BROWSER_MAX_PAGES)
    register_primary(backend, pool)
    robots = RobotsCache(http)
    bucket = HostBucket()
    sessions = SessionManager()
    deps = FetchDeps(
        http=http, pool=pool, robots=robots,
        bucket=bucket, sessions=sessions,
    )
    app["http"] = http
    app["browser"] = pool
    app["robots"] = robots
    app["bucket"] = bucket
    app["sessions"] = sessions
    app["deps"] = deps
    logger.info("spider-http browser backend=%s", backend)

    redis_url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    r = redis_asyncio.from_url(redis_url, decode_responses=False)
    app["redis"] = r
    logger.info("spider-http connected redis=%s", redis_url)

    # Spawn background tasks. They both BRPOP / sleep on Redis, so a
    # broken Redis surfaces as task failures the next time they wake
    # rather than during startup.
    app["dispatcher_task"] = asyncio.create_task(dispatcher(deps, r))
    app["poller_task"] = asyncio.create_task(poller(http, r))
    logger.info("spider-http background tasks started")


async def _cleanup_step(name: str, coro, timeout: float = 5.0) -> bool:
    """Run one cleanup step with a hard timeout. Logs success or failure.

    Returns True if step completed cleanly within `timeout` seconds,
    False if it timed out or raised. Never re-raises — cleanup must
    proceed even if one step fails so we don't leak the rest.
    """
    import time
    started = time.perf_counter()
    try:
        await asyncio.wait_for(coro, timeout=timeout)
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info("cleanup.%s ok elapsed_ms=%.0f", name, elapsed_ms)
        return True
    except asyncio.TimeoutError:
        logger.warning("cleanup.%s TIMEOUT after %.1fs", name, timeout)
        return False
    except Exception as e:
        logger.warning("cleanup.%s error %s: %s", name, type(e).__name__, e)
        return False


async def _on_cleanup(app: web.Application) -> None:
    """Cancel background tasks, then release deps in reverse order.

    Each step is wrapped in a hard timeout so a hung browser child /
    flaky redis connection can't block past docker's stop_grace_period
    (set to 30s in docker-compose.yml; we target <20s here so SIGKILL
    has a buffer). Steps that fail or time out are logged but don't
    prevent later steps from running.
    """
    import time
    cleanup_started = time.perf_counter()
    logger.info("spider-http cleanup begin")

    # 1. Cancel background tasks (cheap, ~instant)
    for key in ("dispatcher_task", "poller_task"):
        t: asyncio.Task | None = app.get(key)
        if t is None or t.done():
            continue
        t.cancel()
        async def _await_cancel(task=t):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        await _cleanup_step(f"cancel.{key}", _await_cancel(), timeout=3.0)

    # 2. Sessions — close any open BrowserContexts before the pool
    # they live on goes away
    sessions: SessionManager | None = app.get("sessions")
    if sessions is not None:
        await _cleanup_step(
            "sessions.close_all", sessions.close_all(), timeout=5.0,
        )

    # 3. Browser pool — slowest step (Chromium child may need 2-5s to die)
    # Plus close any alternate pools the registry spun up.
    pool: CloakBrowserPool | None = app.get("browser")
    if pool is not None:
        await _cleanup_step("browser.shutdown", pool.shutdown(), timeout=10.0)
    await _cleanup_step("browsers.shutdown_all", shutdown_all(), timeout=5.0)

    # 4. Redis — should be near-instant; longer timeout is paranoia
    r = app.get("redis")
    if r is not None:
        await _cleanup_step("redis.aclose", r.aclose(), timeout=3.0)

    # 4. aiohttp ClientSession — close all keep-alive conns
    http: aiohttp.ClientSession | None = app.get("http")
    if http is not None:
        await _cleanup_step("http.close", http.close(), timeout=5.0)

    total_ms = (time.perf_counter() - cleanup_started) * 1000
    logger.info("spider-http cleanup done total_ms=%.0f", total_ms)


def build_app() -> web.Application:
    """Construct the aiohttp app with routes + lifecycle hooks.

    Safe to call from unit tests; the startup hook only fires under
    ``web.run_app`` or ``aiohttp.test_utils.TestServer``.
    """
    app = web.Application()
    register_routes(app)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


if __name__ == "__main__":
    port = int(os.getenv("CRAWLER_PORT", "11700"))
    logger.info("spider-http starting on :%d", port)
    web.run_app(build_app(), host="0.0.0.0", port=port, access_log=None)
