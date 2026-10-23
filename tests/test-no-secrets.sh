#!/usr/bin/env bash
# Fail if any nsec or private-key-shaped material is present in the repo.
# Spec requirement: "Never put private keys in code, logs, or Git."
#
# Allowlisted false positive: the all-'q' dummy key used in
# tests/test-mk-fips-config.sh to exercise the generator's key-shape
# validation. It is not a secret — every character after the prefix is 'q'.
#
# We allowlist that ONE literal rather than weakening the pattern, so a real
# key pasted into the repo still fails the gate. Same discipline as the
# fleet's gitleaks allowlist: narrow, named, self-documenting.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

FAIL=0

# The dummy key used by tests/test-mk-fips-config.sh is nsec1 followed by
# nothing but 'q' characters. Excluding that exact shape is safe: a real key
# has ~57 characters drawn from a 32-symbol alphabet, so the chance of it being
# all-'q' is 32^-57. We drop those lines rather than weakening the pattern, so
# any other nsec (real, or a real one pasted into a test) still fails the gate.
not_dummy() { grep -vE 'nsec1q{20,}'; }

hits=$(grep -rInE 'nsec1[0-9ac-hj-np-z]{20,}' \
         --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ . \
       | not_dummy)
if [ -n "$hits" ]; then
  echo "$hits"
  echo "FAIL: nsec found in repo" >&2; FAIL=1
fi

# 64-char hex that is likely a raw secp256k1 secret, in a key-ish context
if grep -rInE '(private[_-]?key|secret|nsec)[^a-z0-9]{0,4}[0-9a-f]{64}' -i \
     --exclude-dir=.git --exclude-dir=.venv --exclude-dir=__pycache__ . ; then
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
