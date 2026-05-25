"""Mobile-proxy (S8 4G) health helper.

Preflight + on-demand reachability check for the outbound proxy
(``SPIDER_PROXY_URL`` — typically a phone tethered on a 4G/5G carrier,
e.g. the Galaxy-S8 mitmproxy-via-Tailscale gateway documented in
``docs/phone-as-proxy.md``). It confirms the proxy is reachable and
reports its egress IP + geo, so an operator can verify the spider is
actually leaving through the mobile carrier before scaling crawl load
onto it.

Defensive + read-only: it makes ONE GET through the proxy to a public
IP-echo endpoint. It does NOT configure the proxy, install certificates,
or mutate any state — the phone-side setup (mitmproxy, CA trust, UA
rotation) is an operator responsibility, intentionally kept out of this
code path.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any

import aiohttp

from .log import log
from .proxy import aiohttp_request_kwargs, resolve_proxy

# ip-api.com (free tier, HTTP-only) echoes the egress IP + geo. HTTP keeps
# the check working even when the proxy MITMs HTTPS without a trusted CA.
DEFAULT_ECHO_URL = (
    "http://ip-api.com/json/?fields=status,message,query,country,countryCode,timezone,isp"
)
CHECK_TIMEOUT_S = 10.0


@dataclass(slots=True)
class ProxyHealth:
    """Outcome of a single proxy egress probe.

    ``configured`` is False when no proxy is set (no request is made —
    a direct check would just report the host's own IP). ``reachable``
    means the echo endpoint answered through the proxy. ``proxy_url`` is
    credential-redacted so it is safe to surface over HTTP / logs.
    """

    configured: bool
    reachable: bool
    proxy_url: str | None = None
    egress_ip: str | None = None
    country: str | None = None
    country_code: str | None = None
    timezone: str | None = None
    isp: str | None = None
    ms: int | None = None
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def redact_proxy_url(url: str) -> str:
    """Strip any ``user:pass@`` credentials from a proxy URL."""
    try:
        scheme, rest = url.split("://", 1)
    except ValueError:
        return url
    if "@" in rest:
        rest = rest.split("@", 1)[1]
    return f"{scheme}://{rest}"


async def check_proxy(
    http: aiohttp.ClientSession,
    proxy_url: str | None = None,
    *,
    echo_url: str = DEFAULT_ECHO_URL,
    timeout_s: float = CHECK_TIMEOUT_S,
) -> ProxyHealth:
    """Probe the resolved proxy and report egress IP + geo.

    ``proxy_url`` follows the same precedence as the rest of the spider:
    explicit arg > ``SPIDER_PROXY_URL`` env > none. When no proxy is
    configured, returns ``configured=False`` without making any request.
    """
    resolved = resolve_proxy(proxy_url)
    if not resolved:
        return ProxyHealth(configured=False, reachable=False)

    redacted = redact_proxy_url(resolved)
    request_kwargs = aiohttp_request_kwargs(proxy_url)
    timeout = aiohttp.ClientTimeout(total=timeout_s)
    t0 = time.perf_counter()
    try:
        async with http.get(echo_url, timeout=timeout, **request_kwargs) as resp:
            ms = int((time.perf_counter() - t0) * 1000)
            if resp.status != 200:
                health = ProxyHealth(
                    configured=True,
                    reachable=False,
                    proxy_url=redacted,
                    ms=ms,
                    error=f"echo endpoint HTTP {resp.status}",
                )
                log("proxy.health", reachable=False, proxy=redacted, error=health.error)
                return health
            data = await resp.json(content_type=None)
    except Exception as e:
        ms = int((time.perf_counter() - t0) * 1000)
        health = ProxyHealth(
            configured=True,
            reachable=False,
            proxy_url=redacted,
            ms=ms,
            error=f"{type(e).__name__}: {e}",
        )
        log("proxy.health", reachable=False, proxy=redacted, error=health.error)
        return health

    if not isinstance(data, dict):
        return ProxyHealth(
            configured=True,
            reachable=True,
            proxy_url=redacted,
            ms=ms,
            error="echo endpoint returned non-object json",
        )

    if data.get("status") == "fail":
        # The proxy answered (so it is reachable), but the geo lookup itself
        # failed — surface both facts.
        return ProxyHealth(
            configured=True,
            reachable=True,
            proxy_url=redacted,
            egress_ip=data.get("query"),
            ms=ms,
            error=f"geo lookup failed: {data.get('message')}",
        )

    health = ProxyHealth(
        configured=True,
        reachable=True,
        proxy_url=redacted,
        egress_ip=data.get("query"),
        country=data.get("country"),
        country_code=data.get("countryCode"),
        timezone=data.get("timezone"),
        isp=data.get("isp"),
        ms=ms,
    )
    log(
        "proxy.health",
        reachable=True,
        proxy=redacted,
        egress_ip=health.egress_ip,
        country=health.country_code,
        ms=ms,
    )
    return health


__all__ = [
    "CHECK_TIMEOUT_S",
    "DEFAULT_ECHO_URL",
    "ProxyHealth",
    "check_proxy",
    "redact_proxy_url",
]
