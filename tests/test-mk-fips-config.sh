#!/usr/bin/env bash
# Tests for mk-fips-config.sh. Source is our own; verifies idempotence,
# key-shape validation, and the exactly-one-peers invariant.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
GEN="$HERE/scripts/mk-fips-config.sh"
TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
PASS=0; FAIL=0
ok()   { PASS=$((PASS+1)); echo "  ok   - $1"; }
bad()  { FAIL=$((FAIL+1)); echo "  FAIL - $1"; }

# 1. no identity -> exits non-zero
if ! FIPS_NSEC= "$GEN" "$TMPD/a.yaml" >/dev/null 2>&1; then ok "rejects missing identity"; else bad "rejects missing identity"; fi

# 2. malformed identity -> exits non-zero
if ! FIPS_NSEC="notakey" "$GEN" "$TMPD/b.yaml" >/dev/null 2>&1; then ok "rejects malformed identity"; else bad "rejects malformed identity"; fi

# 3. valid identity -> file created, mode 600
FIPS_NSEC="nsec1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq" \
  REGION=us "$GEN" "$TMPD/us.yaml" >/dev/null 2>&1 || bad "generates config"
[ -f "$TMPD/us.yaml" ] && ok "generates config" || bad "generates config"
mode=$(stat -c '%a' "$TMPD/us.yaml" 2>/dev/null)
[ "$mode" = "600" ] && ok "config mode is 600" || bad "config mode is 600 (got $mode)"

# 4. idempotence: same inputs -> byte-identical second run
cp "$TMPD/us.yaml" "$TMPD/us.first"
FIPS_NSEC="nsec1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq" \
  REGION=us "$GEN" "$TMPD/us.yaml" >/dev/null 2>&1
if cmp -s "$TMPD/us.first" "$TMPD/us.yaml"; then ok "idempotent (byte-identical)"; else bad "idempotent (byte-identical)"; fi

# 5. exactly one peers: key
n=$(grep -c '^peers:' "$TMPD/us.yaml")
[ "$n" = "1" ] && ok "exactly one peers: key" || bad "exactly one peers: key (got $n)"

# 6. transports at ROOT level, not nested under node:
if grep -q '^transports:' "$TMPD/us.yaml"; then ok "transports at root level"; else bad "transports at root level"; fi
# 7. and NOT indented under node:
if grep -qE '^  transports:' "$TMPD/us.yaml"; then bad "transports not nested under node"; else ok "transports not nested under node"; fi

# 8. external_addr only present when PUBLIC_IP given
if grep -q 'external_addr' "$TMPD/us.yaml"; then bad "omits external_addr without PUBLIC_IP"; else ok "omits external_addr without PUBLIC_IP"; fi
PUBLIC_IP=1.2.3.4 FIPS_NSEC="nsec1qqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqqq" \
  REGION=uk "$GEN" "$TMPD/uk.yaml" >/dev/null 2>&1
if grep -q 'external_addr: "1.2.3.4:8443"' "$TMPD/uk.yaml"; then ok "emits external_addr with PUBLIC_IP"; else bad "emits external_addr with PUBLIC_IP"; fi

echo "---"
echo "pass=$PASS fail=$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
