#!/usr/bin/env python3
"""Validate chunked transfer against the live fleet.

Baseline being beaten: a whole-object fetch of a 256 KiB blob from the US
replica, 16 clients at once, delivers the complete body to only 6 and ZERO
bytes to the other 10, identically on three consecutive runs.

Here the same object is stored as 64 KiB chunks plus a manifest on the origin,
and 16 concurrent clients each assemble it with bounded concurrency and
per-chunk retry.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cdn.chunked import ChunkedFetcher, split  # noqa: E402

ORIGIN = "http://[fdb0:bf34:d648:96ac:f65a:3fd6:358:710b]:18080"
TARGET = "http://[fdfd:c0e5:3717:6cb1:bb60:de97:987e:7149]:18080"

SIZE = int(sys.argv[1]) if len(sys.argv) > 1 else 256 * 1024
CLIENTS = int(sys.argv[2]) if len(sys.argv) > 2 else 16
CONC = int(sys.argv[3]) if len(sys.argv) > 3 else 4
RETRIES = int(sys.argv[4]) if len(sys.argv) > 4 else 5


def get(url, timeout=90):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def put(url, body, timeout=120):
    req = urllib.request.Request(url, data=body, method="PUT")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def put_retry(base, body, tries=6):
    last = None
    for _ in range(tries):
        try:
            return put(f"{base}/blob", body)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.3)
    raise RuntimeError(f"upload failed: {last}")


def main() -> int:
    data = os.urandom(SIZE // 2) * 2 + b"chunked-validate"
    manifest, chunks = split(data)
    mkey = hashlib.sha256(manifest).hexdigest()

    # Store on the ORIGIN (local to this host, so uploads are not the variable).
    for c in chunks:
        put_retry(ORIGIN, c)
    put_retry(ORIGIN, manifest)

    results, lock = [], threading.Lock()
    barrier = threading.Barrier(CLIENTS)

    def client(i):
        row = {"i": i}
        try:
            barrier.wait(timeout=30)
        except Exception as exc:  # noqa: BLE001
            row.update(ok=False, err=f"barrier:{type(exc).__name__}")
            with lock:
                results.append(row)
            return
        f = ChunkedFetcher(lambda d: get(f"{TARGET}/blob/{d}"),
                           concurrency=CONC, retries=RETRIES)
        t0 = time.perf_counter()
        try:
            body = f.fetch_manifest(get(f"{TARGET}/blob/{mkey}"))
            row.update(ok=body == data, ms=round((time.perf_counter() - t0) * 1000, 1),
                       attempts=f.chunk_attempts, peak=f.peak_inflight)
        except Exception as exc:  # noqa: BLE001
            row.update(ok=False, err=f"{type(exc).__name__}: {str(exc)[:80]}",
                       ms=round((time.perf_counter() - t0) * 1000, 1))
        with lock:
            results.append(row)

    threads = [threading.Thread(target=client, args=(i,)) for i in range(CLIENTS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=900)

    ok = sum(1 for r in results if r.get("ok"))
    out = {
        "object_bytes": len(data),
        "chunks": len(chunks),
        "chunk_size": 64 * 1024,
        "clients": CLIENTS,
        "concurrency_per_client": CONC,
        "retries": RETRIES,
        "complete": ok,
        "failed": CLIENTS - ok,
        "total_chunk_attempts": sum(r.get("attempts", 0) for r in results),
        "max_peak_inflight_per_client": max((r.get("peak", 0) for r in results), default=0),
        "failures": [r for r in results if not r.get("ok")][:5],
    }
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0 if ok == CLIENTS else 1


if __name__ == "__main__":
    raise SystemExit(main())
