#!/usr/bin/env bash
# Acceptance harness: ONE origin, TWO replicas with distinct identities, ONE
# distinct observer, and a GENUINE path asymmetry that is then REVERSED.
#
# ## Topology (real namespaces, real veth pairs, real netem)
#
#   origin ns ── veth ──┐
#                       ├── observer ns ── veth ── replica A ns
#                       └──                veth ── replica B ns
#
# Every node runs in its OWN namespace, including the origin. That is not
# cosmetic: this host's ufw INPUT policy is DROP and does not admit a lab
# subnet, so a host-bound origin times out. Namespacing the origin keeps the
# demo from requiring a host firewall change. Measured during development: with
# a temporary ACCEPT for the lab subnet the same GET returned 200, which is what
# identified the DROP as the cause.
#
# The observer is a DISTINCT identity from both replicas. That is the point the
# earlier withdrawn experiment missed: two same-identity daemons peering with
# each other could never show which replica an observer picks.
#
# Delay is applied on the OBSERVER side of a replica's link, so it models the
# path the viewer actually takes.
#
# ## Why reversal matters
#
# A single run where A is fast only shows A wins. Reversing the asymmetry and
# showing the observer switches to B is what distinguishes measurement-based
# selection from "first in the list won".
#
# Usage: sudo bash scripts/cdn_acceptance.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-python3}"
DELAY_MS="${DELAY_MS:-60}"
OUT="${OUT:-$REPO/evidence/cdn-acceptance.jsonl}"

NS_OBS=cdn-obs
NS_A=cdn-ra
NS_B=cdn-rb
NS_ORIGIN=cdn-origin

PORT_ORIGIN=18080
PORT_A=18081
PORT_B=18082

PIDS=()
cleanup() {
    for p in "${PIDS[@]:-}"; do kill "$p" 2>/dev/null; done
    for ns in "$NS_OBS" "$NS_A" "$NS_B" "$NS_ORIGIN"; do
        ip netns del "$ns" 2>/dev/null
    done
}
trap cleanup EXIT

[ "$(id -u)" = 0 ] || { echo "FAIL: must run as root (netns + netem)" >&2; exit 1; }

echo "=== CDN acceptance: 1 origin, 2 replicas, 1 distinct observer ===" >&2
echo "delay model: ${DELAY_MS}ms on the observer side of ONE replica link" >&2

# ── namespaces ────────────────────────────────────────────────────────────
for ns in "$NS_OBS" "$NS_A" "$NS_B" "$NS_ORIGIN"; do
    ip netns del "$ns" 2>/dev/null
    ip netns add "$ns"
    ip -n "$ns" link set lo up
done

link_pair() { # link_pair <nsA> <ifA> <ipA> <nsB> <ifB> <ipB>
    local a="$1" ia="$2" ipa="$3" b="$4" ib="$5" ipb="$6"
    ip link add "$ia" type veth peer name "$ib"
    ip link set "$ia" netns "$a"
    ip link set "$ib" netns "$b"
    ip -n "$a" addr add "$ipa" dev "$ia"; ip -n "$a" link set "$ia" up
    ip -n "$b" addr add "$ipb" dev "$ib"; ip -n "$b" link set "$ib" up
}

link_pair "$NS_ORIGIN" veth-oh 10.99.0.1/24 "$NS_OBS" veth-ho 10.99.0.2/24
link_pair "$NS_A" veth-a 10.98.0.1/24 "$NS_OBS" veth-oa 10.98.0.2/24
link_pair "$NS_B" veth-b 10.97.0.1/24 "$NS_OBS" veth-ob 10.97.0.2/24

# Each replica also needs a DIRECT link to the origin: a replica fills its cache
# from the origin, and the observer namespace is not a router. Without these, a
# replica-side miss fails with "Network is unreachable" and every fetch 502s —
# measured during development, and easy to misread as a cache bug.
link_pair "$NS_ORIGIN" veth-ora 10.95.0.1/24 "$NS_A" veth-or-a 10.95.0.2/24
link_pair "$NS_ORIGIN" veth-orb 10.94.0.1/24 "$NS_B" veth-or-b 10.94.0.2/24

for tgt in 10.98.0.1 10.97.0.1 10.99.0.1; do
    ip netns exec "$NS_OBS" ping -c1 -W2 "$tgt" >/dev/null 2>&1 \
        || { echo "FAIL: observer cannot reach $tgt" >&2; exit 1; }
done
# Replicas must reach the origin on their own uplink before any fill is attempted.
ip netns exec "$NS_A" ping -c1 -W2 10.95.0.1 >/dev/null 2>&1 \
    || { echo "FAIL: replica A cannot reach origin" >&2; exit 1; }
ip netns exec "$NS_B" ping -c1 -W2 10.94.0.1 >/dev/null 2>&1 \
    || { echo "FAIL: replica B cannot reach origin" >&2; exit 1; }
echo "topology up; observer reaches origin + both replicas" >&2

# ── start nodes (each in its own namespace) ───────────────────────────────
mkdir -p "$(dirname "$OUT")"

ip netns exec "$NS_ORIGIN" "$PY" "$REPO/scripts/cdn_node.py" origin \
    --port "$PORT_ORIGIN" > /tmp/cdn-origin.log 2>&1 &
PIDS+=($!)

ip netns exec "$NS_A" "$PY" "$REPO/scripts/cdn_node.py" replica \
    --port "$PORT_A" --upstream "http://10.95.0.1:$PORT_ORIGIN" \
    > /tmp/cdn-ra.log 2>&1 &
PIDS+=($!)

ip netns exec "$NS_B" "$PY" "$REPO/scripts/cdn_node.py" replica \
    --port "$PORT_B" --upstream "http://10.94.0.1:$PORT_ORIGIN" \
    > /tmp/cdn-rb.log 2>&1 &
PIDS+=($!)

# Readiness probe from the observer: never sleep blind.
ready=0
for _ in $(seq 1 30); do
    if ip netns exec "$NS_OBS" "$PY" -c "
import sys, urllib.request
try:
    for u in ('http://10.99.0.1:$PORT_ORIGIN/health',
              'http://10.98.0.1:$PORT_A/health',
              'http://10.97.0.1:$PORT_B/health'):
        urllib.request.urlopen(u, timeout=1).read()
except Exception:
    sys.exit(1)
" 2>/dev/null; then
        ready=1
        echo "all three nodes healthy" >&2
        break
    fi
    sleep 0.5
done
[ "$ready" = 1 ] || {
    echo "FAIL: nodes never became healthy" >&2
    tail -5 /tmp/cdn-origin.log /tmp/cdn-ra.log /tmp/cdn-rb.log >&2
    exit 1
}

run_phase() { # run_phase <label> <slow_side: a|b|none>
    local label="$1" slow="$2"
    ip netns exec "$NS_OBS" tc qdisc del dev veth-oa root 2>/dev/null
    ip netns exec "$NS_OBS" tc qdisc del dev veth-ob root 2>/dev/null
    if [ "$slow" = a ]; then
        ip netns exec "$NS_OBS" tc qdisc add dev veth-oa root netem delay "${DELAY_MS}ms"
    elif [ "$slow" = b ]; then
        ip netns exec "$NS_OBS" tc qdisc add dev veth-ob root netem delay "${DELAY_MS}ms"
    fi
    ip netns exec "$NS_OBS" "$PY" "$REPO/scripts/cdn_measure.py" \
        --origin-url "http://10.99.0.1:$PORT_ORIGIN" \
        --replica-a "http://10.98.0.1:$PORT_A" --npub-a npub_replica_a \
        --replica-b "http://10.97.0.1:$PORT_B" --npub-b npub_replica_b \
        --label "$label" >> "$OUT"
}

: > "$OUT"
run_phase "phase1_A_fast" b     # A fast, B delayed
run_phase "phase2_reversed" a   # delay MOVED to A -> observer must switch to B

echo "--- results ---" >&2
cat "$OUT" >&2
echo "OK: acceptance run complete -> $OUT" >&2
