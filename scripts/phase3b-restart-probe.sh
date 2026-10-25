#!/usr/bin/env bash
# WARNING: EXPERIMENTAL HISTORICAL HARNESS; NOT ACCEPTANCE EVIDENCE.
# NOT APPROVED FOR RERUN. See docs/EVIDENCE-CORRECTION.md for safety hazards
# and withdrawn conclusions. Comments/output below preserve historical claims.
# Phase 3b — THE decisive experiment: restart into an existing peering.
#
# ## Why this variant, and not just "two daemons, one identity"
#
# core/src/proto/fmp/core.rs:494-517 shows the epoch comparison is guarded by
# `if snap.has_existing_peer`. A simultaneous cold start has NO existing peer
# record, so both sides take the `Promote` branch and nothing tears down. The
# predicted `RestartThenPromote` requires a peer record whose STORED epoch
# differs from the incoming one.
#
# So the trap is: establish A<->B under the shared identity, let A record B's
# epoch, then RESTART B so it draws a fresh random epoch while A still holds the
# old one. If the source reading is right, A sees "peer that restarted" and tears
# the session down. If it does not, the mechanism is narrower still.
#
# Usage: sudo bash scripts/phase3b-restart-probe.sh
set -uo pipefail

WORK="${P3_WORK:-/home/c08r4d0r/worktrees/archy-fips-bump/image-recipe/build/p3-restart}"
NS_A=p3r-a
NS_B=p3r-b
FIPS=/usr/bin/fips
CTL=/usr/bin/fipsctl
HITS=0

cleanup() {
    for n in a b; do
        [ -f "$WORK/$n/daemon.pid" ] && kill "$(cat "$WORK/$n/daemon.pid")" 2>/dev/null
    done
    pkill -f "$WORK" 2>/dev/null
    ip netns del "$NS_A" 2>/dev/null
    ip netns del "$NS_B" 2>/dev/null
}
trap cleanup EXIT
[ "$(id -u)" = 0 ] || { echo "FAIL: must run as root"; exit 1; }

echo "=== Phase 3b: restart into an existing peering (shared identity) ==="
echo

mkdir -p "$WORK/a" "$WORK/b"
"$CTL" keygen --dir "$WORK/shared" --force >/dev/null 2>&1 || { echo "FAIL: keygen"; exit 1; }
SHARED_NSEC="$(sed -n '1p' "$WORK/shared/fips.key")"
SHARED_NPUB="$(tr -d '[:space:]' < "$WORK/shared/fips.pub")"
echo "shared identity: ${SHARED_NPUB:0:20}..."; echo

for ns in "$NS_A" "$NS_B"; do ip netns del "$ns" 2>/dev/null; ip netns add "$ns"; done
ip link add veth-a type veth peer name veth-b
ip link set veth-a netns "$NS_A"; ip link set veth-b netns "$NS_B"
ip -n "$NS_A" addr add 10.45.0.1/24 dev veth-a; ip -n "$NS_A" link set veth-a up
ip -n "$NS_B" addr add 10.45.0.2/24 dev veth-b; ip -n "$NS_B" link set veth-b up
ip -n "$NS_A" link set lo up; ip -n "$NS_B" link set lo up
for ns in "$NS_A" "$NS_B"; do ip netns exec "$ns" ip tuntap add dev fips0 mode tun 2>/dev/null || true; done

mk_cfg() {
    cat > "$1/fips.yaml" <<YAML
node:
  identity:
    nsec: "$SHARED_NSEC"
  lookup:
    backoff_base_secs: 1
    backoff_max_secs: 5
  rendezvous:
    lan:
      enabled: false
  retry:
    base_interval_secs: 1
    max_backoff_secs: 5
    max_retries: 60
tun:
  enabled: true
  name: fips0
  mtu: 1280
dns:
  enabled: false
transports:
  udp:
    bind_addr: "0.0.0.0:$2"
peers:
  - npub: "$SHARED_NPUB"
    alias: "shared-peer"
    addresses:
      - transport: udp
        addr: "$3"
    connect_policy: auto_connect
YAML
}
mk_cfg "$WORK/a" 2121 "10.45.0.2:2122"
mk_cfg "$WORK/b" 2122 "10.45.0.1:2121"

for n in a b; do
    mkdir -p "$WORK/$n/run-fips" "$WORK/$n/etc-fips"
    : > "$WORK/$n/etc-fips/peers.allow"
    printf '# probe: default-open\n' > "$WORK/$n/etc-fips/peers.deny"
done

launch() { # launch <ns> <dir>
    local ns="$1" dir="$2"
    ip netns exec "$ns" unshare --mount --propagation private bash -c "
        mount --bind '$dir/run-fips' /run/fips 2>/dev/null || true
        mount --bind '$dir/etc-fips' /etc/fips 2>/dev/null || true
        exec '$FIPS' --config '$dir/fips.yaml'
    " >> "$dir/daemon.log" 2>&1 &
    echo $! > "$dir/daemon.pid"
}
p3_ctl() { local ns="$1" dir="$2"; shift 2
    ip netns exec "$ns" unshare --mount --propagation private bash -c "
        mount --bind '$dir/run-fips' /run/fips 2>/dev/null || true
        mount --bind '$dir/etc-fips' /etc/fips 2>/dev/null || true
        exec '$CTL' \"\$@\"
    " _ "$@"
}
peer_ct() { p3_ctl "$1" "$2" show peers 2>/dev/null | grep -cE '"authenticated_at_ms"'; }

# ── Step 1: establish the peering ─────────────────────────────────────────
launch "$NS_A" "$WORK/a"
launch "$NS_B" "$WORK/b"
echo "step 1: both up under the shared identity; waiting for a stable peering..."
A_CT=0; B_CT=0
for i in $(seq 1 30); do
    A_CT="$(peer_ct "$NS_A" "$WORK/a")"; B_CT="$(peer_ct "$NS_B" "$WORK/b")"
    [ "$A_CT" -gt 0 ] && [ "$B_CT" -gt 0 ] && break
    sleep 2
done
echo "  A peers=$A_CT  B peers=$B_CT"
[ "$A_CT" -gt 0 ] && [ "$B_CT" -gt 0 ] || {
    echo "FAIL: never peered; the restart test needs an existing peering first"
    exit 1; }

# Confirm A now HOLDS B's epoch in its peer record -- this precondition is the
# whole reason the prediction should fire on restart.
echo "  A peer record (looking for the stored epoch):"
p3_ctl "$NS_A" "$WORK/a" show peers 2>/dev/null \
    | grep -iE '"(epoch|remote_epoch|npub|connectivity)"' | head -6 | sed 's/^/    /'
echo

# ── Step 2: restart B -> new random startup epoch, A still holds the old ──
echo "step 2: restarting B (draws a FRESH startup epoch) while A keeps its record..."
kill "$(cat "$WORK/b/daemon.pid")" 2>/dev/null
sleep 3
: > "$WORK/b/daemon.log"
launch "$NS_B" "$WORK/b"

echo "  sampling 30x5s (150s) for the predicted restart-teardown signal..."
for i in $(seq 1 30); do
    printf '.'
    sleep 5
done
echo; echo

for n in a b; do
    for p in 'Peer restart detected during promotion' 'stale FSP session after peer restart' 'stale active peer'; do
        if grep -qi -- "$p" "$WORK/$n/daemon.log" 2>/dev/null; then
            echo "HIT: node $n: '$p'"; grep -i -- "$p" "$WORK/$n/daemon.log" | head -2; HITS=$((HITS+1))
        fi
    done
done

echo "── final state ──"
for n in a b; do
    ns="$NS_A"; d="$WORK/a"; [ "$n" = b ] && { ns="$NS_B"; d="$WORK/b"; }
    echo "  node $n: peers=$(peer_ct "$ns" "$d")"
    p3_ctl "$ns" "$d" show tree 2>/dev/null \
        | grep -E '"(is_root|parent_switches|unstable_root)"' | head -4 | sed 's/^/    /'
done
echo
echo "================================================"
if [ "$HITS" -gt 0 ]; then
    echo "VERDICT: PASS (prediction CONFIRMED) — restart into an existing"
    echo "         shared-identity peering DOES trigger the 'peer restarted'"
    echo "         teardown. Shared-identity anycast is NOT viable."
else
    echo "VERDICT: prediction NOT reproduced even on the restart path."
    echo "         The epoch mechanism did not fire in this configuration."
fi
echo "================================================"
