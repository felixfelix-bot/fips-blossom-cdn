#!/usr/bin/env bash
# One command to run every test in this repo. Exits non-zero on any failure.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

RC=0
run() {
  local name="$1"; shift
  echo "== $name =="
  if "$@"; then
    echo "-- $name OK"
  else
    echo "-- $name FAILED" >&2
    RC=1
  fi
  echo
}

run "no-secrets"      bash tests/test-no-secrets.sh
run "mk-fips-config"  bash tests/test-mk-fips-config.sh
run "sync-regions"    python3 tests/test-sync-regions.py
run "cdn-proxy"       python3 tests/test-cdn-proxy.py
run "bash-syntax"     bash -n scripts/health-check.sh scripts/mk-fips-config.sh
run "python-syntax"   python3 -m py_compile scripts/cdn-proxy.py scripts/sync-regions.py

echo "================================"
if [ "$RC" -eq 0 ]; then echo "ALL TESTS PASSED"; else echo "FAILURES PRESENT"; fi
exit "$RC"
