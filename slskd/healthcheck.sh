#!/bin/bash
# slskd's stock healthcheck only asks "is the web server up?". slskd can sit
# there for days, web UI fine, logged out of Soulseek, and Docker calls it
# healthy, so autoheal never acts. This check asks the question that matters:
#
#   web API dead                          -> unhealthy (restart)
#   logged in to Soulseek                 -> healthy
#   logged out, gluetun unreachable       -> unhealthy: gluetun was restarted and this
#                                            container is stuck in its old, dead network
#                                            namespace. Only a restart re-attaches it.
#   logged out, VPN tunnel down           -> healthy: nothing slskd can do, gluetun heals
#                                            itself and slskd's VPN integration reconnects.
#   logged out, VPN fine                  -> ask slskd to reconnect, report unhealthy.
#                                            Docker needs several failures in a row before it
#                                            flags the container, so a reconnect that works
#                                            never causes a restart.

PORT="${SLSKD_HTTP_PORT:-5030}"
KEY="${SLSKD_API_KEY##*;}"            # tolerate the "role=..;cidr=..;key" form
GLUETUN="${SLSKD_VPN_GLUETUN_URL:-http://localhost:8000}"
API="http://localhost:${PORT}"

get() { wget -q -T 5 -t 1 -O - "$@"; }

get "${API}/health" >/dev/null || { echo "slskd web API is not responding"; exit 1; }

# without an API key we cannot look any deeper; behave like the stock check
[ -n "$KEY" ] || { echo "ok (SLSKD_API_KEY not set, Soulseek state not checked)"; exit 0; }

state="$(get --header "X-API-Key: ${KEY}" "${API}/api/v0/server")" \
  || { echo "cannot read Soulseek state from the API (wrong SLSKD_API_KEY?)"; exit 1; }

if jq -e '.isConnected and .isLoggedIn' >/dev/null 2>&1 <<<"$state"; then
  echo "ok: $(jq -r '.state' <<<"$state")"
  exit 0
fi

if [ "${SLSKD_VPN:-false}" = "true" ]; then
  vpn="$(get --header "X-API-Key: ${SLSKD_VPN_GLUETUN_API_KEY:-}" "${GLUETUN}/v1/publicip/ip")" \
    || { echo "gluetun control server unreachable: stale network namespace, restart required"; exit 1; }
  if [ -z "$(jq -r '.public_ip // empty' <<<"$vpn" 2>/dev/null)" ]; then
    echo "VPN tunnel is down, waiting for gluetun"
    exit 0
  fi
fi

if jq -e '.isConnecting or .isLoggingIn' >/dev/null 2>&1 <<<"$state"; then
  echo "Soulseek login in progress"
else
  get --method=PUT --body-data='' --header "X-API-Key: ${KEY}" "${API}/api/v0/server" >/dev/null
  echo "logged out of Soulseek ($(jq -r '.state' <<<"$state")), reconnect requested"
fi
exit 1
