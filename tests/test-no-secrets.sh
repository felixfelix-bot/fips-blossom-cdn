#!/usr/bin/env bash
# Fail if any nsec or private-key-shaped material is present in the repo.
# Spec requirement: "Never put private keys in code, logs, or Git."
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

FAIL=0

# nsec bech32 (test or prod)
if grep -rInE 'nsec1[0-9ac-hj-np-z]{20,}' --exclude-dir=.git --exclude-dir=.venv . ; then
  echo "FAIL: nsec found in repo" >&2; FAIL=1
fi

# 64-char hex that is likely a raw secp256k1 secret, in a key-ish context
if grep -rInE '(private[_-]?key|secret|nsec)[^a-z0-9]{0,4}[0-9a-f]{64}' -i \
     --exclude-dir=.git --exclude-dir=.venv . ; then
  echo "FAIL: private-key-shaped literal found in repo" >&2; FAIL=1
fi

# Common committed-env-file mistakes
for f in .env fips.key fips.pub; do
  if git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
    echo "FAIL: $f is tracked by git" >&2; FAIL=1
  fi
done

if [ "$FAIL" -eq 0 ]; then
  echo "OK: no secrets found"
fi
exit "$FAIL"
