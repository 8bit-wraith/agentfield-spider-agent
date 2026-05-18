#!/usr/bin/env python3
"""Network-reuse probe for browser pools + shared aiohttp session.

This smoke probe validates the anti-bot baseline expectation that
sequential requests to the same origin reuse an existing transport
connection (HTTP/2 multiplexing or HTTP/1.1 keep-alive) instead of
performing a fresh TLS handshake for every request.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import aiohttp

import spider_server
from spider.browsers import make_pool, normalize_backend
from spider.sessions import SessionManager

DEFAULT_TARGETS = (
    "https://www.howsmyssl.com/a/check",
    "https://httpbin.org/headers",
)
DEFAULT_BACKENDS = ("cloak", "lightpanda")
DEFAULT_ENFORCE_TARGETS = ("https://httpbin.org/headers",)


@dataclass(slots=True)
class ProbeOutcome:
    kind: str
    name: str
    status: str
    details: dict[str, Any]


def _same_doc_target(response_url: str, target_url: str) -> bool:
    ru = urlsplit(response_url)
    tu = urlsplit(target_url)
    return ru.scheme == tu.scheme and ru.netloc == tu.netloc and ru.path == tu.path


def _evaluate_reuse(samples: list[dict[str, Any]]) -> dict[str, Any]:
    conn_ids = [s.get("connection_id") for s in samples if s.get("connection_id") is not None]
    protocols = [s.get("protocol") for s in samples if s.get("protocol")]
    unique_ids = sorted(set(conn_ids))
    reused = len(conn_ids) >= 3 and len(unique_ids) < len(conn_ids)
    return {
        "reused": reused,
        "connection_ids": conn_ids,
        "unique_connection_ids": unique_ids,
        "protocols": protocols,
    }


async def _probe_aiohttp_target(target: str) -> ProbeOutcome:
    stats = {"created": 0, "reused": 0}
    trace = aiohttp.TraceConfig()

    async def _on_conn_create_start(
        _session: aiohttp.ClientSession,
        _ctx: object,
        _params: object,
    ) -> None:
        stats["created"] += 1

    async def _on_conn_reuse(
        _session: aiohttp.ClientSession,
        _ctx: object,
        _params: object,
    ) -> None:
        stats["reused"] += 1

    trace.on_connection_create_start.append(_on_conn_create_start)
    trace.on_connection_reuseconn.append(_on_conn_reuse)

    sess = spider_server._build_http_session(trace_configs=[trace])
    statuses: list[int] = []
    try:
        for _ in range(3):
            async with sess.get(target, timeout=aiohttp.ClientTimeout(total=20)) as resp:
                statuses.append(resp.status)
                await resp.read()
    except Exception as e:
        return ProbeOutcome(
            kind="aiohttp",
            name=target,
            status="failed",
            details={
                "error": f"{type(e).__name__}: {e}",
                "statuses": statuses,
                **stats,
            },
        )
    finally:
        await sess.close()

    ok = statuses == [200, 200, 200] and stats["created"] == 1 and stats["reused"] >= 2
    return ProbeOutcome(
        kind="aiohttp",
        name=target,
        status="passed" if ok else "failed",
        details={"statuses": statuses, **stats},
    )


async def _probe_backend_target(
    backend: str,
    session_mgr: SessionManager,
    session_id: str,
    pool: Any,
    target: str,
) -> ProbeOutcome:
    samples: list[dict[str, Any]] = []
    try:
        for i in range(3):
            sample: dict[str, Any] = {"request_index": i}
            async with session_mgr.lease_page(session_id, pool=pool) as page:
                events: list[dict[str, Any]] = []
                cdp = None
                try:
                    cdp = await page.context.new_cdp_session(page)
                    await cdp.send("Network.enable")

                    def _on_response(
                        params: dict[str, Any],
                        *,
                        events_ref: list[dict[str, Any]] = events,
                        target_url: str = target,
                    ) -> None:
                        if params.get("type") != "Document":
                            return
                        response = params.get("response") or {}
                        response_url = str(response.get("url") or "")
                        if not _same_doc_target(response_url, target_url):
                            return
                        events_ref.append(
                            {
                                "url": response_url,
                                "connection_id": response.get("connectionId"),
                                "protocol": response.get("protocol"),
                                "remote_ip": response.get("remoteIPAddress"),
                            }
                        )

                    cdp.on("Network.responseReceived", _on_response)
                except Exception as e:
                    sample["cdp_error"] = f"{type(e).__name__}: {e}"

                await page.goto(target, wait_until="domcontentloaded", timeout=45_000)
                await asyncio.sleep(0.2)
                if events:
                    sample.update(events[-1])
                if cdp is not None:
                    with contextlib.suppress(Exception):
                        await cdp.detach()
            samples.append(sample)
    except Exception as e:
        return ProbeOutcome(
            kind="browser",
            name=f"{backend}:{target}",
            status="failed",
            details={"error": f"{type(e).__name__}: {e}", "samples": samples},
        )

    eval_data = _evaluate_reuse(samples)
    return ProbeOutcome(
        kind="browser",
        name=f"{backend}:{target}",
        status="passed" if eval_data["reused"] else "failed",
        details={"samples": samples, **eval_data},
    )


async def _probe_backend(backend: str, targets: tuple[str, ...]) -> list[ProbeOutcome]:
    pool = make_pool(normalize_backend(backend))
    session_mgr = SessionManager()
    out: list[ProbeOutcome] = []
    try:
        try:
            info = await session_mgr.create(pool=pool, backend_label=backend, name="network-reuse-smoke")
        except Exception as e:
            out.append(
                ProbeOutcome(
                    kind="browser",
                    name=f"{backend}:bootstrap",
                    status="skipped",
                    details={"error": f"{type(e).__name__}: {e}"},
                )
            )
            return out

        for target in targets:
            out.append(await _probe_backend_target(backend, session_mgr, info.id, pool, target))
        return out
    finally:
        await session_mgr.close_all()
        await pool.shutdown()


async def _run(args: argparse.Namespace) -> int:
    targets = tuple(args.targets or DEFAULT_TARGETS)
    backends = tuple(args.backends or DEFAULT_BACKENDS)
    required_backends = set(args.require_backends)
    enforce_targets = set(args.enforce_targets or DEFAULT_ENFORCE_TARGETS)
    outcomes: list[ProbeOutcome] = []

    for target in targets:
        outcomes.append(await _probe_aiohttp_target(target))

    for backend in backends:
        outcomes.extend(await _probe_backend(backend, targets))

    summary = {
        "outcomes": [
            {
                "kind": o.kind,
                "name": o.name,
                "status": o.status,
                "details": o.details,
            }
            for o in outcomes
        ]
    }
    print(json.dumps(summary, indent=2))

    failures: list[str] = []
    for o in outcomes:
        if o.status == "failed":
            if o.kind == "aiohttp" and o.name in enforce_targets:
                failures.append(f"{o.kind}:{o.name}")
            elif o.kind == "browser":
                _backend, _sep, target = o.name.partition(":")
                if target in enforce_targets:
                    failures.append(f"{o.kind}:{o.name}")
        if o.status == "skipped" and o.kind == "browser":
            backend_name = o.name.split(":", 1)[0]
            if backend_name in required_backends:
                failures.append(f"{o.kind}:{o.name} (required backend skipped)")

    return 1 if failures else 0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Probe HTTP/TLS connection reuse.")
    p.add_argument(
        "--target",
        dest="targets",
        action="append",
        default=[],
        help="Target URL to probe. May be passed multiple times.",
    )
    p.add_argument(
        "--backend",
        dest="backends",
        action="append",
        default=[],
        help="Browser backend to probe (cloak/lightpanda). May be passed multiple times.",
    )
    p.add_argument(
        "--require-backend",
        dest="require_backends",
        action="append",
        default=[],
        help="Fail if this backend is unavailable and gets skipped.",
    )
    p.add_argument(
        "--enforce-target",
        dest="enforce_targets",
        action="append",
        default=[],
        help=(
            "Only failed probes for these target URLs make the script exit non-zero. "
            "By default, only httpbin is enforced."
        ),
    )
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
