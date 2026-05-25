# Phone as 4G/5G proxy for the spider

Datacenter IPs get bot-flagged. Mobile carrier IPs (4G/5G) rarely do
— Cloudflare's "bot" verdict differs by ASN, and a phone tethered on
a mobile network looks like a real human session to most anti-bot
heuristics.

The spider supports an outbound proxy via the `SPIDER_PROXY_URL` env
var. This guide walks the easiest setup paths for routing the spider's
HTTP + browser traffic through a phone's mobile connection.

You only need this when scraping sites that block datacenter IPs.
Most of the URL dispatcher (GitHub, HN, Reddit) speaks to the official
APIs which don't care about IP, so the proxy is most useful for the
browser-rendered path.

## Three setup paths

| Path | Phone | Pros | Cons |
|---|---|---|---|
| **A. Termux + SSH tunnel** | Android | Easiest; no extra services; survives reboot with `termux-boot` | Battery drain; phone has to be unlocked + plugged in |
| **B. Tailscale exit node** | Android (works on iOS too with caveats) | Mesh networking; works from anywhere on the tailnet; encrypted | Requires Tailscale account; bandwidth metered through Tailscale's DERP if direct fails |
| **C. WiFi hotspot to a small Linux box (Pi/old laptop)** | Any | Phone just tethers; the proxy lives on the box | Two devices; more moving parts |

All three end with the spider seeing a `SPIDER_PROXY_URL` it can
connect to. The differences are who runs the proxy daemon.

---

## Path A — Termux SOCKS5 over SSH (Android, recommended)

The spider will SSH to the phone and use SSH's `-D` flag to open a
local SOCKS5 port. Traffic egresses through the phone's mobile
connection.

### One-time setup on the phone

```bash
# Install Termux from F-Droid (the Play Store version is outdated).
# Inside Termux:

pkg update && pkg install openssh

# Set a password for incoming ssh:
passwd

# Start sshd:
sshd
# (Listens on port 8022 by default.)

# Find the phone's IP on your LAN (assuming you've turned on WiFi
# hotspot / connected to the same LAN as the spider host):
ifconfig | grep inet | grep -v 127.0.0.1
# Note the IP, e.g. 192.168.43.123
```

To keep sshd running after reboot, install `termux-boot` from F-Droid
and create `~/.termux/boot/start-sshd` with:

```bash
#!/data/data/com.termux/files/usr/bin/sh
sshd
```
(`chmod +x` it.)

### On the spider host

```bash
# Open a persistent SOCKS5 tunnel through the phone:
ssh -p 8022 -fNg -D 0.0.0.0:1080 u0_a123@192.168.43.123

# Replace u0_a123 with `whoami` output from Termux.
# -f = background, -N = no remote command, -g = allow other hosts to
# connect to the forwarded port, -D 0.0.0.0:1080 = SOCKS5 on local 1080.
```

Then in the spider's `.env`:

```
SPIDER_PROXY_URL=socks5://127.0.0.1:1080
```

Restart the spider. Verify with:

```bash
curl --socks5 127.0.0.1:1080 -fsS https://ifconfig.me
# Should print your phone's mobile carrier IP, not your home WAN IP.
```

### Caveats

- Phone must be unlocked and connected to mobile data (turn off WiFi
  or set carrier as preferred for that app via Android settings ->
  Network -> Mobile network -> Allowed networks for data).
- Termux + sshd uses ~30-50 MB RAM, ~negligible CPU.
- Battery drain is the limiting factor; keep the phone on a charger.

---

## Path B — Tailscale exit node

Tailscale lets you route the spider's traffic through the phone as if
the phone were a VPN gateway, with auto-routing on the mesh.

### On the phone

1. Install Tailscale (Android Play Store / F-Droid).
2. Log in with the same account / tailnet as the spider host.
3. Settings -> "Use as exit node" -> enable.
4. In the Tailscale admin console, approve the phone's exit-node
   advertisement.

### On the spider host

There are two ways to wire this up. The simpler one is to set the
phone as the exit node for the entire spider host:

```bash
sudo tailscale up --exit-node=phone-friendly-name
```

But that routes everything through the phone, not just the spider.
If you want only the spider to egress via the phone, run a per-
container SOCKS proxy:

```bash
# Install a tiny SOCKS proxy that lives on the spider host and
# routes only its connections through the tailscale exit node:
docker run -d --name socks-via-phone \
  --net=host \
  serjs/go-socks5-proxy
# Then `SPIDER_PROXY_URL=socks5://127.0.0.1:1080` and set the route
# at the OS level so connections from socks-via-phone egress through
# tailscale's exit-node interface.
```

### Caveats

- iOS Tailscale exit-node support is limited; works best with
  Android.
- Requires a Tailscale subscription only if you go over the free
  tier's device count.

---

## Path C — WiFi hotspot + Linux box as proxy

If the phone runs hot in Termux or you want a more "set and forget"
setup, dedicate a small Linux box (Raspberry Pi, old laptop, mini
PC) to running the proxy. The phone just provides internet to that
box via WiFi tethering.

### On the phone

Enable WiFi hotspot. Set a password. Note the SSID.

### On the small box

```bash
# Connect to the phone's hotspot.
# Then install something like tinyproxy:
sudo apt install tinyproxy
sudo systemctl enable --now tinyproxy

# Edit /etc/tinyproxy/tinyproxy.conf:
#   Port 8888
#   Allow <spider-host's LAN IP>
sudo systemctl restart tinyproxy
```

### On the spider host

```
SPIDER_PROXY_URL=http://<pi-lan-ip>:8888
```

### Caveats

- Phone has to stay near the box (within WiFi hotspot range).
- The box's own outbound goes through the phone; if you want the
  box's regular updates to use your home network, configure routes
  carefully.

---

## Path D — Galaxy-S8 mitmproxy over a Tailscale mesh IP (recommended for 4G)

This is the setup behind our "S8 4G mobile proxy". It is structurally
different from A–C: there is **no SSH tunnel and no host-wide exit
node**. The phone runs `mitmproxy` (inside proot-Debian inside Termux)
and is reachable *directly* on its Tailscale mesh IP. The spider just
points `SPIDER_PROXY_URL` at `http://<tailnet-ip>:<port>` and it works
from anywhere on the tailnet.

|  | A (sshd) | B (exit node) | C (Pi) | **D (this one)** |
|---|---|---|---|---|
| Phone runs | Termux + sshd | Tailscale exit node | WiFi hotspot | Termux + proot Debian + mitmproxy |
| Reach via | SSH `-D` SOCKS5 | host-wide exit-node | Pi LAN IP | **Tailscale mesh IP, direct** |
| `SPIDER_PROXY_URL` | `socks5://127.0.0.1:1080` | (host-wide) | `http://<pi-lan-ip>:8888` | **`http://<tailnet-ip>:<port>`** (e.g. `http://100.122.115.94:12345`) |
| UA spoofing | spider-side | spider-side | spider-side | **proxy-side** (mitmproxy addon rotates mobile UAs per request) |
| HTTPS interception | no | no | no | **yes** (mitmproxy CA must be trusted on the spider host) |

The mesh-IP-direct approach means no tunnel layer to babysit and no
exit-node config that changes the spider host's whole routing.

### On the spider host

```
SPIDER_PROXY_URL=http://<tailnet-ip>:<port>
```

That's it for the happy path. Confirm with the built-in egress check
(see [Verification](#verification) below):

```bash
curl -fsS http://localhost:11700/proxy/health | jq .
# {"configured": true, "reachable": true, "egress_ip": "...", "country_code": "DE", ...}
```

### Caveats (operator responsibilities — NOT automated)

These steps are deliberately left out of the spider's code. They are
device- and trust-store changes the operator performs manually after
reviewing them:

1. **UA spoofing happens server-side.** The mitmproxy addon rewrites
   `User-Agent` per request from a mobile-UA rotation list, so the
   spider's own UA setting is overridden. Anything that needs to
   identify itself to a peer (e.g. AgentField internal heartbeats)
   should bypass the proxy.
2. **HTTPS interception requires CA trust.** mitmproxy performs MITM.
   The spider host must trust `mitmproxy-ca-cert.pem` (pulled from the
   device) or CloakBrowser throws cert errors. ⚠️ Installing a MITM CA
   lets that proxy decrypt **all** HTTPS from the host — only do this
   on a host dedicated to scraping, and never point the proxy at
   traffic you wouldn't want intercepted.
3. **The proxy lives on Android's sandbox edge.** If Termux is killed
   by the low-memory killer, the proxy goes down silently. Use
   `GET /proxy/health` (or `scripts/check-proxy.sh`) as a watchdog /
   readiness gate before scaling crawl load onto it.

### Reference

Full device-side runbook (mitmproxy addon, Termux/proot bootstrap, CA
export) is maintained by the operator outside this repo. This repo only
ships the *spider-side* wiring (`SPIDER_PROXY_URL`) and the
`/proxy/health` egress check.

---

## Verification

The fastest check is the built-in egress probe — it makes one request
through the configured proxy and reports the carrier IP + geo:

```bash
curl -fsS http://localhost:11700/proxy/health | jq .
# {
#   "configured": true,
#   "reachable": true,
#   "proxy_url": "http://100.122.115.94:12345",   # credentials redacted
#   "egress_ip": "84.17.52.9",
#   "country": "Germany", "country_code": "DE",
#   "timezone": "Europe/Berlin", "isp": "Vodafone GmbH",
#   "ms": 412
# }
```

`reachable: false` (HTTP 502) means the proxy is set but the spider
couldn't egress through it — check the `error` field. `configured:
false` means `SPIDER_PROXY_URL` is unset. From the AgentField surface
the same check is the `spider-agent.proxy_health` skill;
`scripts/check-proxy.sh` wraps it for the smoke ladder.

The spider also logs `proxy=true` on `browser.opened` when it launches
CloakBrowser through a proxy. For HTTP fetchers there's no per-call
log, but you can confirm with:

```bash
# From any host that can reach the spider:
curl -fsS -X POST http://localhost:11700/fetch/sync \
  -H 'Content-Type: application/json' \
  -d '{"url": "https://ifconfig.me", "use_browser": true}'
```

The `text` field in the response should be your phone's carrier IP.

For a per-call override (useful for testing one URL against the
proxy without changing the env):

```bash
curl -fsS -X POST http://localhost:11700/fetch/sync \
  -H 'Content-Type: application/json' \
  -d '{
    "url": "https://github.com/zakirullin/files.md",
    "proxy": "socks5://127.0.0.1:1080"
  }'
```

(GitHub URLs use the HTTP fetcher, so per-call `proxy` works.
For the browser path, the env-level `SPIDER_PROXY_URL` applies.)

## When NOT to use the proxy

- GitHub / HN / Reddit fetchers hit official APIs. Those APIs don't
  care about IP (within rate limits). Routing API calls through a
  metered mobile connection wastes bandwidth.
- Internal-network URLs (the SSRF gate already blocks loopback /
  private IPs, but if you ever loosen the gate, the proxy would
  break those calls).

The intended target is the browser-rendered path on sites with
aggressive bot detection. Keep the proxy enabled for that, and
consider `proxy=""` (empty string) as a per-call override to
forcibly bypass it for known-friendly hosts.
