# Network reuse audit (HTTP/2 keep-alive + TLS session reuse)

Issue: `VIB-37`  
Date: 2026-05-18  
Surface: browser pools (`cloak`, `lightpanda`) + shared `aiohttp` session (`FetchDeps.http`)

## Goal

Verify that sequential fetches to the same origin reuse transport state
(HTTP/2 streams / keep-alive connections) instead of opening a fresh
TCP+TLS connection per request.

## Method

Because Wireshark/mitmproxy are not available in this cloud runtime, the
audit used runtime-native telemetry:

- **Browser pools (cloak/lightpanda):**
  - Create one `SessionManager` session (`BrowserContext` kept alive).
  - Perform 3 sequential `page.goto(...)` navigations per host.
  - Capture CDP `Network.responseReceived` (`connectionId`, `protocol`,
    `remoteIPAddress`) for the document response.
- **aiohttp (`FetchDeps.http` equivalent):**
  - Use `TraceConfig` hooks (`on_connection_create_start`,
    `on_connection_reuseconn`) during 3 sequential GETs per host.

Targets:

- `https://www.howsmyssl.com/a/check`
- `https://httpbin.org/headers`

## Results

### CloakBrowser pool (session-backed context reuse)

| Target | Protocol | Connection IDs across 3 requests | Reuse |
|---|---|---|---|
| `httpbin.org/headers` | h2 | `80, 80, 80` | ✅ yes |
| `howsmyssl.com/a/check` | h2 | `35, 53, 61` | ❌ no |

Interpretation:

- For `httpbin`, Chromium reuses a single HTTP/2 connection across all
  3 requests inside one `BrowserContext`.
- `howsmyssl` consistently rotated connection IDs in this environment.
  This appears origin-specific, not pool-specific, since the same pattern
  was seen in `aiohttp`.

### aiohttp shared session (`FetchDeps.http`)

| Target | Statuses | Connections created | Reused | Reuse |
|---|---|---:|---:|---|
| `httpbin.org/headers` | `200,200,200` | 1 | 2 | ✅ yes |
| `howsmyssl.com/a/check` | `200,200,200` | 3 | 0 | ❌ no |

Interpretation:

- `httpbin` shows expected keep-alive behavior (`1 create + 2 reuses`).
- `howsmyssl` appears to force reconnects (or rapidly close sessions)
  from this runner.

### Lightpanda pool

Runtime measurement was **not possible** in this environment because no
Lightpanda CDP endpoint was available:

- `connect_over_cdp("http://lightpanda:9222")` → `ENOTFOUND lightpanda`

Code path audit:

- `LightpandaPool` keeps a long-lived `_browser` connection and reuses
  context `contexts[0]` when available.
- `SessionManager` creates a persistent `BrowserContext` and reuses it
  across leases, same as cloak.

So implementation shape supports reuse, but runtime verification requires
an active Lightpanda sidecar.

## Code changes shipped

### 1) Explicit keep-alive tuning for shared aiohttp session

`spider_server.py` now builds the long-lived HTTP session with explicit
connector options:

- `TCPConnector(keepalive_timeout=..., force_close=False, ttl_dns_cache=..., limit=..., limit_per_host=...)`
- env knobs:
  - `SPIDER_HTTP_KEEPALIVE_TIMEOUT_S` (default `45`)
  - `SPIDER_HTTP_CONN_LIMIT` (default `200`)
  - `SPIDER_HTTP_CONN_LIMIT_PER_HOST` (default `50`)
  - `SPIDER_HTTP_DNS_CACHE_TTL_S` (default `300`)
  - `SPIDER_HTTP_ENABLE_CLEANUP_CLOSED` (default `1`)

This makes connection reuse explicit and stable even if aiohttp defaults
change upstream.

### 2) Regression smoke script

Added:

- `scripts/check-network-reuse.sh`
- `scripts/probe_network_reuse.py`

The smoke probe:

- runs 3 sequential requests per target for aiohttp + each browser backend
- reports connection IDs + protocols
- fails CI only for enforced targets (default: `https://httpbin.org/headers`)
- can require specific backends via env

Usage:

```bash
./scripts/check-network-reuse.sh
```

Strict mode (fail if Lightpanda unavailable):

```bash
SMOKE_REUSE_REQUIRE_BACKENDS=lightpanda ./scripts/check-network-reuse.sh
```

## Recommendations

1. Keep `httpbin` as the enforced CI reuse target (stable behavior).
2. Keep `howsmyssl` as advisory telemetry only (good visibility, but
   origin behavior can force reconnects).
3. In Lightpanda-enabled environments, run:

```bash
SMOKE_REUSE_REQUIRE_BACKENDS=lightpanda ./scripts/check-network-reuse.sh
```

to turn its probe from "skip allowed" into "hard gate".
