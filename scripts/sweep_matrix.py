#!/usr/bin/env python3
"""Two-dimensional sweep: chunk size x client concurrency.

The previous sweep ran one object size and two concurrency levels and showed a
monotonic trend (bigger chunk = faster). That is not enough to claim there is an
optimum, so this widens both axes to find out whether a peak exists.

Deterministic data: chunks are derived from a seeded SHAKE-256 stream so the
replica can be seeded locally (instant) while the client recomputes the same
hashes. No setup traffic crosses the mesh, so the numbers are delivery-only.

Modes:
  seed  -- run ON the replica host; stores every chunk for every size locally
  fetch -- run ON the client host; measures concurrent full-object fetches

Usage:
  python3 scripts/sweep_matrix.py seed
  python3 scripts/sweep_matrix.py fetch 6,16,32
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cdn.chunked import ChunkedFetcher  # noqa: E402

LOCAL = "http://127.0.0.1:18080"
TARGET = "http://[fdfd:c0e5:3717:6cb1:bb60:de97:987e:7149]:18080"

OBJECT = 1024 * 1024                       # 1 MiB
SIZES_KIB = [16, 32, 64, 128, 256, 512, 1024]
CONCURRENCY_PER_CLIENT = 4
RETRIES = 6
RUN_DEADLINE_S = 240


def payload(size: int) -> bytes:
    return hashlib.shake_256(f"cdnsweep:{size}".encode()).digest(size)


def chunks_for(size: int, chunk: int) -> list[bytes]:
    d = payload(size)
    return [d[i:i + chunk] for i in range(0, len(d), chunk)] or [b""]


def hashes_for(size: int, chunk: int) -> list[str]:
    return [hashlib.sha256(c).hexdigest() for c in chunks_for(size, chunk)]


def get(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def put(url, body, timeout=30):
    req = urllib.request.Request(url, data=body, method="PUT")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def seed() -> int:
    t0 = time.perf_counter()
    total = 0
    for kib in SIZES_KIB:
        for c in chunks_for(OBJECT, kib * 1024):
            put(f"{LOCAL}/blob", c)
            total += len(c)
        print(f"seeded {kib} KiB chunks", flush=True)
    print(json.dumps({"seeded_bytes": total, "s": round(time.perf_counter() - t0, 1)}))
    return 0


def run_level(size: int, chunk: int, clients: int) -> dict:
    hashes = hashes_for(size, chunk)
    data = payload(size)
    results, lock = [], threading.Lock()
    barrier = threading.Barrier(clients)
    deadline = time.perf_counter() + RUN_DEADLINE_S

    def client(i):
        row = {"i": i, "ok": False, "attempts": 0}
        try:
            barrier.wait(timeout=30)
        except Exception:  # noqa: BLE001
            with lock:
                results.append(row)
            return
        t0 = time.perf_counter()
        f = ChunkedFetcher(lambda h: get(f"{TARGET}/blob/{h}"),
                           concurrency=CONCURRENCY_PER_CLIENT,
                           retries=RETRIES, backoff=0.1)
        try:
            row["ok"] = f.fetch_hashes(hashes, len(data)) == data
        except Exception as exc:  # noqa: BLE001
            row["err"] = type(exc).__name__
        row["attempts"] = f.chunk_attempts
        row["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        with lock:
            results.append(row)

    threads = [threading.Thread(target=client, args=(i,)) for i in range(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=max(1, deadline - time.perf_counter()))

    ok_times = [r["ms"] for r in results if r.get("ok")]
    return {
        "chunk_kib": chunk // 1024,
        "clients": clients,
        "chunks": len(hashes),
        "complete": sum(1 for r in results if r.get("ok")),
        "median_ok_s": round(statistics.median(ok_times) / 1000, 1) if ok_times else None,
        "mean_attempts": round(sum(r["attempts"] for r in results) / max(1, len(results)), 1),
        "wasted_ratio": round(
            sum(r["attempts"] for r in results) * (chunk / 1024)
            / max(1, len(results) * (size / 1024)), 2),
    }


def fetch(levels: list[int]) -> int:
    out = {"object_bytes": OBJECT, "concurrency_per_client": CONCURRENCY_PER_CLIENT,
           "retries": RETRIES, "rows": []}
    path = "/tmp/sweep_matrix.json"
    for kib in SIZES_KIB:
        for n in levels:
            row = run_level(OBJECT, kib * 1024, n)
            out["rows"].append(row)
            print(json.dumps(row), flush=True)
            with open(path, "w") as fh:
                json.dump(out, fh, indent=2, sort_keys=True)
    print(json.dumps({"written": path, "rows": len(out["rows"])}))
    return 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "fetch"
    if mode == "seed":
        raise SystemExit(seed())
    levels = [int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2 else [6, 16, 32]
    raise SystemExit(fetch(levels))
