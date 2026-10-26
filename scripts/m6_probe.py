#!/usr/bin/env python3
"""Diagnose M6: why do some concurrent clients fail while coalescing works?

Records per-client outcome, the exception TYPE (the fleet run only counted
errors, which is why ten failures could not be attributed), and elapsed time.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import urllib.request

ORIGIN = "http://[fdb0:bf34:d648:96ac:f65a:3fd6:358:710b]:18080"
TARGET = "http://[fdfd:c0e5:3717:6cb1:bb60:de97:987e:7149]:18080"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 16
# Object size matters: coalescing deduplicates the FILL, but every client still
# receives the whole object. At 256 KiB x 16 = 4 MiB of egress the transatlantic
# link, not the coalescer, decides the outcome.
SIZE = int(sys.argv[1]) if len(sys.argv) > 1 else 256 * 1024


def get(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def put(url, body, timeout):
    req = urllib.request.Request(url, data=body, method="PUT")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def main() -> int:
    body = os.urandom(SIZE) + b"m6-probe"
    key = hashlib.sha256(body).hexdigest()
    t0 = time.perf_counter()
    put(f"{ORIGIN}/blob", body, timeout=180)
    put_ms = round((time.perf_counter() - t0) * 1000, 1)

    before = json.loads(get(f"{TARGET}/stats", timeout=15))
    rows = []
    lock = threading.Lock()
    barrier = threading.Barrier(N)

    def client(i):
        row = {"i": i}
        try:
            barrier.wait(timeout=30)
        except Exception as exc:  # noqa: BLE001
            row.update(outcome="BARRIER", exc=type(exc).__name__)
            with lock:
                rows.append(row)
            return
        t = time.perf_counter()
        try:
            data = get(f"{TARGET}/blob/{key}", timeout=300)
            row.update(outcome="ok", ms=round((time.perf_counter() - t) * 1000, 1),
                       ok_hash=hashlib.sha256(data).hexdigest() == key)
        except Exception as exc:  # noqa: BLE001
            row.update(outcome="fail", exc=type(exc).__name__,
                       detail=str(exc)[:120],
                       ms=round((time.perf_counter() - t) * 1000, 1))
        with lock:
            rows.append(row)

    ts = [threading.Thread(target=client, args=(i,)) for i in range(N)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=330)

    after = json.loads(get(f"{TARGET}/stats", timeout=15))
    out = {
        "put_ms": put_ms,
        "object_bytes": len(body),
        "clients": N,
        "ok": sum(1 for r in rows if r.get("outcome") == "ok"),
        "fail": sum(1 for r in rows if r.get("outcome") == "fail"),
        "barrier": sum(1 for r in rows if r.get("outcome") == "BARRIER"),
        "exc_types": sorted({r.get("exc") for r in rows if r.get("exc")}),
        "rows": sorted(rows, key=lambda r: r["i"]),
        "upstream_fetches_delta": after.get("upstream_fetches", 0) - before.get("upstream_fetches", 0),
        "coalesced_waiters_delta": after.get("coalesced_waiters", 0) - before.get("coalesced_waiters", 0),
    }
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
