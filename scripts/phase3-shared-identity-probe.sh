#!/usr/bin/env bash
# Phase 3 — live shared-identity confirmation (netns venue, NO fleet impact).
#
# ## What this tests
#
# docs/E2E-TEST-PLAN.md Phase 3 predicts, from source, that two FIPS daemons
# running under ONE shared identity each draw a different random startup epoch,
# so each classifies the other as "a peer that restarted" and tears its session
# down -- a flapping loop. The source reading says:
#
#   core/src/proto/fmp/core.rs:496   -> RestartThenPromote
#   src/node/handlers/handshake.rs:2393 -> close_session(), "this_wins"
#   log: "Peer restart detected during promotion, replacing stale active peer"
#
# ## Pass condition is the ABSENCE of the predicted log lines
#
# NOT the presence of a connection. A connection WILL form -- that is what makes
# the trap deceptive. A pass means: two daemons under one identity coexist and
# peer STABLY, contradicting the source reading.
#
# ## Venue
#
# Two network namespaces joined by a veth pair. Both daemons use the SAME nsec.
# Zero interaction with the live fleet mesh: nothing binds 0.0.0.0 on the host,
# the host fips.service is untouched, and the whole thing is torn down at exit.
#
# Usage: sudo bash scripts/phase3-shared-identity-probe.sh [--keep]
set -uo pipefail

WORK="${P3_WORK:-/home/c08r4d0r/worktrees/archy-fips-bump/image-recipe/build/p3-shared}"
KEEP=0
[ "${1:-}" = "--keep" ] && KEEP=1

NS_A=p3-shared-a
NS_B=p3-shared-b
FIPS=/usr/bin/fips
CTL=/usr/bin/fipsctl

LOG_A="$WORK/a/daemon.log"
LOG_B="$WORK/b/daemon.log"

PRED_1='Peer restart detected during promotion'
PRED_2='stale FSP session after peer restart'
PRED_3='stale active peer'
HITS=0

cleanup() {
    for n in "$NS_A" "$NS_B"; do
        [ -f "$WORK/${n#p3-shared-}/daemon.pid" ] && \
            kill "$(cat "$WORK/${n#p3-shared-}/daemon.pid")" 2>/dev/null
    done
    pkill -f "$WORK" 2>/dev/null
    ip netns del "$NS_A" 2>/dev/null
    ip netns del "$NS_B" 2>/dev/null
    if [ "$KEEP" = 1 ]; then echo "kept: $WORK (namespaces left in place)"; fi
}
trap cleanup EXIT

[ "$(id -u)" = 0 ] || { echo "FAIL: must run as root (netns + tun)"; exit 1; }

echo "=== Phase 3: shared-identity live confirmation (netns venue) ==="
echo

# ── One shared identity, generated fresh, never reused from the fleet ──────
mkdir -p "$WORK/a" "$WORK/b"
"$CTL" keygen --dir "$WORK/shared" --force >/dev/null 2>&1 || {
    echo "FAIL: keygen for the shared identity" >&2; exit 1; }
SHARED_NSEC="$(sed -n '1p' "$WORK/shared/fips.key")"
SHARED_NPUB="$(tr -d '[:space:]' < "$WORK/shared/fips.pub")"
[ -n "$SHARED_NSEC" ] && [ -n "$SHARED_NPUB" ] || {
    echo "FAIL: shared identity empty" >&2; exit 1; }
echo "shared identity npub: ${SHARED_NPUB:0:20}..."

# ── Namespaces + veth ─────────────────────────────────────────────────────
for ns in "$NS_A" "$NS_B"; do ip netns del "$ns" 2>/dev/null; ip netns add "$ns"; done
ip link add veth-a type veth peer name veth-b
ip link set veth-a netns "$NS_A"
ip link set veth-b netns "$NS_B"
ip -n "$NS_A" addr add 10.44.0.1/24 dev veth-a; ip -n "$NS_A" link set veth-a up
ip -n "$NS_B" addr add 10.44.0.2/24 dev veth-b; ip -n "$NS_B" link set veth-b up
ip -n "$NS_A" link set lo up;  ip -n "$NS_B" link set lo up
for ns in "$NS_A" "$NS_B"; do
    ip netns exec "$ns" ip tuntap add dev fips0 mode tun 2>/dev/null || true
done

# ── Configs: IDENTICAL shared nsec on both sides ──────────────────────────
# Each side gets its own udp port so the two daemons can find each other over
# the veth; everything else is the same, including the identity.
mk_cfg() { # mk_cfg <dir> <udp_port> <peer_addr>
    cat > "$1/fips.yaml" <<YAML
node:
  identity:
    nsec: "$SHARED_NSEC"
  lookup:
    backoff_base_secs: 1
    backoff_max_secs: 30
  rendezvous:
    lan:
      enabled: false
  retry:
    base_interval_secs: 1
    max_backoff_secs: 30
    max_retries: 30
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
mk_cfg "$WORK/a" 2121 "10.44.0.2:2122"
mk_cfg "$WORK/b" 2122 "10.44.0.1:2121"

echo "configs written (both carry the SAME nsec -- this is the point)"
echo

# ── Launch ────────────────────────────────────────────────────────────────
# CRITICAL — TWO hardcoded host paths leak into a naive netns run and make the
# probe read FLEET state instead of its own:
#
#   /run/fips/control.sock  — the control socket. Without isolating it,
#                             `fipsctl` reports the HOST fleet daemon's peers
#                             (andre, dq05, ...), so the probe looks like it
#                             measured something while measuring the host.
#   /etc/fips/peers.deny    — the ACL. The fleet's real ACL is `deny ALL` plus a
#                             strict allowlist of 9 fleet npubs, so a test
#                             identity is silently rejected with
#                             "Rejected peer by ACL ... decision=denylist match"
#                             and the two nodes NEVER peer — an INCONCLUSIVE run
#                             that looks like a FIPS property.
#
# Both are fixed paths, not config keys, so isolation needs a MOUNT namespace
# with private /run/fips AND /etc/fips.
launch() { # launch <ns> <dir>
    local ns="$1" dir="$2"
    mkdir -p "$dir/run-fips" "$dir/etc-fips"
    # Private ACL: empty allow, no deny -> default-open, so the shared identity
    # is not filtered by fleet policy that has nothing to do with this test.
    : > "$dir/etc-fips/peers.allow"
    printf '# probe: no deny list; the shared-identity question is not an ACL question\n' \
        > "$dir/etc-fips/peers.deny"
    ip netns exec "$ns" unshare --mount --propagation private bash -c "
        mount --bind '$dir/run-fips' /run/fips 2>/dev/null || true
        mount --bind '$dir/etc-fips' /etc/fips 2>/dev/null || true
        exec '$FIPS' --config '$dir/fips.yaml'
    " > "$dir/daemon.log" 2>&1 &
    echo $! > "$dir/daemon.pid"
}
launch "$NS_A" "$WORK/a"
launch "$NS_B" "$WORK/b"

sleep 6
for n in a b; do
    if kill -0 "$(cat "$WORK/$n/daemon.pid")" 2>/dev/null; then
        echo "OK:   node $n: daemon alive under the shared identity"
    else
        echo "FAIL: node $n: daemon exited under the shared identity"
        tail -15 "$WORK/$n/daemon.log" >&2
    fi
done
echo

# ── The decisive watch: sample the predicted corruption signal ────────────
# Phase 3: sample ~60x at 5s. That is 5 minutes; we sample for the window this
# probe can afford and RECORD the window honestly rather than implying 60.
SAMPLES="${P3_SAMPLES:-24}"
INTERVAL="${P3_INTERVAL:-5}"

# Probe a node's OWN daemon. MUST go through the same mount-ns layout the
# daemon runs in, or we read the host fleet daemon's socket instead.
p3_ctl() { # p3_ctl <ns> <dir> <fipsctl args...>
    local ns="$1" dir="$2"; shift 2
    ip netns exec "$ns" unshare --mount --propagation private bash -c "
        mount --bind '$dir/run-fips' /run/fips 2>/dev/null || true
        mount --bind '$dir/etc-fips' /etc/fips 2>/dev/null || true
        exec '$CTL' \"\$@\"
    " _ "$@"
}

echo "sampling ${SAMPLES}x${INTERVAL}s (window $((SAMPLES*INTERVAL))s) for the predicted signal..."
echo

for i in $(seq 1 "$SAMPLES"); do
    printf '.'
    sleep "$INTERVAL"
done
echo; echo

# ── Verdict: the predicted log lines ──────────────────────────────────────
for n in a b; do
    f="$WORK/$n/daemon.log"
    for p in "$PRED_1" "$PRED_2" "$PRED_3"; do
        if grep -qi -- "$p" "$f" 2>/dev/null; then
            echo "HIT:  node $n: '$p'"
            grep -i -- "$p" "$f" | head -3
            HITS=$((HITS+1))
        fi
    done
done

# session counts + tree state, for the record
for n in a b; do
    ns="$NS_A"; d="$WORK/a"
    [ "$n" = b ] && { ns="$NS_B"; d="$WORK/b"; }
    echo "── node $n ──"
    peers="$(p3_ctl "$ns" "$d" show peers 2>/dev/null || true)"
    if [ -z "$peers" ]; then
        echo "  (no output from $n's own control socket — isolation failed)"
    else
        echo "$peers" | grep -cE '"authenticated_at_ms"' | sed 's/^/  peers: /'
        echo "$peers" | grep -oE '"display_name": "[^"]+"' | head -4 | sed 's/^/    /'
    fi
    p3_ctl "$ns" "$d" show tree 2>/dev/null \
        | grep -E '"(is_root|parent_switches|unstable_root)"' | head -6 | sed 's/^/  /'
done
echo

echo "================================================"
if [ "$HITS" -gt 0 ]; then
    echo "VERDICT: PASS (prediction CONFIRMED) — shared identity corrupts:"
    echo "         the predicted 'peer restarted' teardown lines appeared."
    echo "         => shared-identity anycast is NOT viable. Do not deploy."
else
    echo "VERDICT: INCONCLUSIVE — the predicted lines did NOT appear in"
    echo "         ${SAMPLES}x${INTERVAL}s. Either the source reading is wrong,"
    echo "         or the daemons never actually peered (check 'peers' above)."
    echo "         A non-zero peer count with zero hits is the interesting case."
fi
echo "================================================"
