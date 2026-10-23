#!/usr/bin/env bash
# Health check for one CDN region. Targets the REGIONAL identity, never the
# shared one (GAP-4): the shared plane is an experiment and would flap.
#
# Exit 0 = healthy, 1 = unhealthy. Prints one line of key=value evidence and a
# JSON line, so the caller never has to trust a bare exit code.
set -uo pipefail
PROXY_PORT="${PROXY_PORT:-3200}"
REGION="${REGION:-unknown}"
FAIL=0
KV=()

kv() { KV+=("$1=$2"); }

# 1. FIPS daemon
if systemctl is-active --quiet fips; then kv fips up; else kv fips down; FAIL=1; fi

# 2. Transports operational (0 transports = a silently dead overlay)
T=$(fipsctl show status 2>/dev/null | sed -n 's/.*transports:[[:space:]]*\([0-9]*\).*/\1/p' | head -1)
kv transports "${T:-unknown}"
[ "${T:-0}" -ge 1 ] 2>/dev/null || FAIL=1

# 3. Blossom reachable
if curl -sf --max-time 4 -o /dev/null "http://127.0.0.1:${PROXY_PORT}/health"; then
  kv proxy up
else
  kv proxy down; FAIL=1
fi

# 4. Blob count + last sync, straight from the proxy
H=$(curl -sf --max-time 4 "http://127.0.0.1:${PROXY_PORT}/health" 2>/dev/null)
BLOBS=$(printf '%s' "$H" | sed -n 's/.*"blobs": *\([0-9]*\).*/\1/p')
kv blobs "${BLOBS:-0}"

# 5. Sync timer enabled (persistence across reboots, GAP-7)
if systemctl is-enabled --quiet fips-cdn-sync.timer; then kv sync_timer enabled; else kv sync_timer disabled; FAIL=1; fi

echo "region=${REGION} ${KV[*]}"
printf '{"region":"%s","ok":%s,"%s"}\n' \
  "$REGION" "$([ $FAIL -eq 0 ] && echo true || echo false)" \
  "$(printf '%s,' "${KV[@]}" | sed 's/,$//' | sed 's/\([a-z_]*\)=\([^ ,]*\)/"\1":"\2"/g')"
exit $FAIL
