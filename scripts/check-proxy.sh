#!/usr/bin/env bash
# Check: GET /proxy/health — verify the outbound proxy (S8 4G gateway)
# egress IP + geo. Skips cleanly (PASS) when no SPIDER_PROXY_URL is set,
# since a direct check would just report the host's own IP.

source "$(dirname "$0")/_lib.sh"

echo "═══ I. GET ${BASE_URL}/proxy/health ═══"
CODE=$(hcurl -o /tmp/spider_proxy_health.json -w "%{http_code}" "${BASE_URL}/proxy/health" || echo "000")
echo "  HTTP $CODE"
BODY=$(cat /tmp/spider_proxy_health.json 2>/dev/null || echo '{}')
echo "$BODY" | jq . 2>/dev/null || echo "$BODY"
echo

CONFIGURED=$(echo "$BODY" | jq -r '.configured // false')
REACHABLE=$(echo "$BODY" | jq -r '.reachable // false')

case "$CODE" in
    200)
        if [[ "$CONFIGURED" != "true" ]]; then
            warn "no SPIDER_PROXY_URL configured — proxy egress check skipped"
            exit 0
        fi
        IP=$(echo "$BODY" | jq -r '.egress_ip // "?"')
        GEO=$(echo "$BODY" | jq -r '.country_code // "?"')
        ok "proxy reachable; egress ${IP} (${GEO})"
        ;;
    502)
        if [[ "$REACHABLE" == "false" ]]; then
            fail "proxy configured but unreachable: $(echo "$BODY" | jq -r '.error // "?"')"
        else
            fail "proxy check failed (HTTP 502)"
        fi
        exit 2
        ;;
    000|503|504)
        fail "spider-http unreachable at ${BASE_URL}. Check: docker compose logs spider-http"
        exit 2
        ;;
    *)
        fail "unexpected HTTP $CODE — see /tmp/spider_proxy_health.json"
        exit 2
        ;;
esac
