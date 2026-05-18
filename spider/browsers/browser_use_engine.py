"""browser-use engine wrapper (stub for v1, deeper integration in next PR).

`browser-use <https://github.com/browser-use/web-ui>`_ is an LLM-driven
browser-automation library (langchain-backed). It pairs a Chromium
instance with an LLM that decides what to click / type / scroll based
on a natural-language task.

Architecture target
-------------------

Two deployment modes:

  embedded   spider-agent's Python process drives browser-use directly.
             Heavy: pulls in playwright + langchain + browser-use deps.
             Best for "one task per call" use cases.

  sidecar    A separate browser-use/web-ui container runs Chromium +
             exposes CDP + VNC. spider-agent calls its HTTP API.
             Heavier overall but the LLM workload is isolated from
             the spider process. Best for many-concurrent-agents.

This module exposes:

  get_chrome_cdp_url()   The CDP endpoint to drive a browser via
                         Playwright's connect_over_cdp. Backed either
                         by the sidecar's BROWSER_USE_CDP_URL or by
                         spawning a local Chrome (embedded mode, v2).
  get_vnc_url()          The VNC endpoint for visualization, if the
                         deployment runs a browser-use/web-ui sidecar
                         with VNC enabled.
  browser_act(task, ...) Run a browser-use Agent on a natural-language
                         task. v1 stub returns "not yet wired"; v2 PR
                         fills this in.

Why a stub for v1
-----------------

Full browser-use integration drags in langchain + transformers + the
browser-use library itself (heavy). We don't want to gate the entire
spider-agent image on that, and the integration testing is non-trivial
(it needs a real LLM endpoint + browser).

For v1 we just expose the connection URLs as a convenience — operators
running the browser-use/web-ui sidecar can grab CDP / VNC URLs via
spider-agent's HTTP API. Deeper integration (a ``browser_act`` skill
that runs an Agent task) lands in the next PR.
"""
from __future__ import annotations

import os
from typing import Any

from ..log import log


# Env vars consumed here. Documented in .env.example.
BROWSER_USE_CDP_URL = os.environ.get(
    "BROWSER_USE_CDP_URL", "http://browser-use:9222",
)
BROWSER_USE_VNC_URL = os.environ.get(
    "BROWSER_USE_VNC_URL", "vnc://browser-use:5900",
)
BROWSER_USE_HTTP_URL = os.environ.get(
    "BROWSER_USE_HTTP_URL", "http://browser-use:8080",
)
BROWSER_USE_DEFAULT_MODEL = os.environ.get(
    "BROWSER_USE_DEFAULT_MODEL", "openai/gpt-4o",
)


def get_chrome_cdp_url() -> str:
    """Return the CDP URL operators / clients can use to drive Chrome.

    Sourced from ``BROWSER_USE_CDP_URL``. Defaults to the compose
    service name; in k3s the operator overrides via the ConfigMap.

    The URL is NOT health-checked here — callers should hit
    ``/json/version`` on it to confirm the browser-use sidecar is up.
    The ``/chrome-conn`` route does this check.
    """
    return BROWSER_USE_CDP_URL


def get_vnc_url() -> str:
    """Return the VNC URL for visual debugging of the browser-use sidecar.

    Sourced from ``BROWSER_USE_VNC_URL``. Default is ``vnc://browser-use:5900``
    — only meaningful when the operator runs the browser-use/web-ui
    container with VNC enabled (it does by default).
    """
    return BROWSER_USE_VNC_URL


async def probe_chrome_alive() -> tuple[bool, str]:
    """Cheap CDP smoke check.

    Hits ``GET /json/version`` on the configured CDP URL. Returns
    ``(ok, detail)`` — detail is a one-line description suitable for
    the /health surface or operator logs.
    """
    import httpx

    url = BROWSER_USE_CDP_URL.rstrip("/") + "/json/version"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(url)
            if r.status_code != 200:
                return False, f"cdp probe non-200: {r.status_code}"
            data = r.json()
            return True, f"cdp ok: browser={data.get('Browser', '?')}"
    except Exception as e:  # noqa: BLE001
        return False, f"cdp probe {type(e).__name__}: {e}"


# ─── stub: browser_act ──────────────────────────────────────────────
async def browser_act(
    task: str,
    *,
    model: str | None = None,
    session_id: str | None = None,
    max_steps: int = 25,
) -> dict[str, Any]:
    """Run a browser-use Agent on ``task``. STUB for v1.

    The next PR wires this up against the browser-use library:
      from browser_use import Agent
      agent = Agent(task=task, llm=<openrouter-langchain client>)
      result = await agent.run(max_steps=max_steps)

    For v1, returns an error envelope describing the not-yet-wired
    state so operators get a clear signal rather than a 500.
    """
    log("browser_act", state="stub", task=task[:80])
    return {
        "status": "error",
        "error": (
            "browser_act is a v1 stub. Install browser-use via the "
            "[browser-use] extra and wait for the next PR that wires "
            "the Agent loop."
        ),
        "task": task,
        "model": model or BROWSER_USE_DEFAULT_MODEL,
        "session_id": session_id,
        "max_steps": max_steps,
    }


__all__ = [
    "BROWSER_USE_CDP_URL",
    "BROWSER_USE_DEFAULT_MODEL",
    "BROWSER_USE_HTTP_URL",
    "BROWSER_USE_VNC_URL",
    "browser_act",
    "get_chrome_cdp_url",
    "get_vnc_url",
    "probe_chrome_alive",
]
