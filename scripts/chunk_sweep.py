#!/usr/bin/env python3
"""Chunk-size sweep on the live fleet: when is which chunk size right?

Two costs pull in opposite directions:

  * Small chunks pay a round trip per chunk. On a ~600 ms RTT path the fixed
    cost dominates: 64 KiB chunks can spend more time waiting than transferring.
  * Large chunks lose more when a flow stalls. The path delivers zero bytes to
    contending flows, so a lost 1 MiB chunk costs 16x a lost 64 KiB chunk.

This measures both: sequential per-chunk cost (round-trip overhead) and
concurrent full-object completion (loss exposure), then reports the crossing.

Run from a host with real mesh paths.
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

ORIGIN = "http://[fdb0:bf34:d648:96ac:f65a:3fd6:358:710b]:18080"
TARGET = "http://[fdfd:c0e5:3717:6cb1:bb60:de97:987e:7149]:18080"

OBJECT = 512 * 1024
SIZES = ([int(x) for x in sys.argv[2].split(",")] if len(sys.argv) > 2
         else [16, 32, 64, 128, 256, 512])          # KiB
CLIENTS = int(sys.argv[1]) if len(sys.argv) > 1 else 12
CONC = 4
RETRIES = 5
SAMPLES = 3


def get(url, timeout=15):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def put(url, body, timeout=180):
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


def run_size(chunk_bytes: int, data: bytes) -> dict:
    chunks = [data[i:i + chunk_bytes] for i in range(0, len(data), chunk_bytes)]
    hashes = [hashlib.sha256(c).hexdigest() for c in chunks]
    for c in chunks:
        put_retry(ORIGIN, c)

    # Warm the replica: otherwise each chunk fetch also triggers an upstream
    # fill over the same long path, and the sweep measures fill contention
    # instead of delivery. Sequential, so it stays under the flow ceiling.
    for h in hashes:
        for _ in range(6):
            try:
                if hashlib.sha256(get(f"{TARGET}/blob/{h}", timeout=60)).hexdigest() == h:
                    break
            except Exception:  # noqa: BLE001
                time.sleep(0.2)

    # --- sequential: per-chunk cost, which is where small chunks lose ---
    sample_key = hashes[len(hashes) // 2]
    lat = []
    for _ in range(SAMPLES):
        # A single stalled flow must not abort the sweep: retry the sample.
        for attempt in range(6):
            try:
                t0 = time.perf_counter()
                body = get(f"{TARGET}/blob/{sample_key}")
                ms = (time.perf_counter() - t0) * 1000
                assert hashlib.sha256(body).hexdigest() == sample_key
                lat.append(ms)
                break
            except Exception:  # noqa: BLE001
                if attempt == 5:
                    lat.append(float("nan"))
                time.sleep(0.2)
    per_chunk_ms = statistics.median(lat)
    # effective throughput of one sequential stream at this chunk size
    seq_mib_s = (chunk_bytes / (1024 * 1024)) / (per_chunk_ms / 1000)

    # --- concurrent: full-object completion under contention ---
    results, lock = [], threading.Lock()
    barrier = threading.Barrier(CLIENTS)

    def client(i):
        # Use the tested fetcher rather than a hand-rolled thread pool: it
        # propagates failures instead of swallowing them in a thread, and its
        # retry policy is the one we actually ship.
        row = {"i": i, "ok": False, "attempts": 0}
        try:
            barrier.wait(timeout=30)
        except Exception:  # noqa: BLE001
            with lock:
                results.append(row)
            return
        t0 = time.perf_counter()
        f = ChunkedFetcher(lambda h: get(f"{TARGET}/blob/{h}"),
                           concurrency=CONC, retries=RETRIES, backoff=0.1)
        try:
            got = f.fetch_hashes(hashes, len(data))
            row["ok"] = got == data
        except Exception as exc:  # noqa: BLE001
            row["err"] = type(exc).__name__
        row["attempts"] = f.chunk_attempts
        row["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        with lock:
            results.append(row)

    threads = [threading.Thread(target=client, args=(i,)) for i in range(CLIENTS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=900)

    complete = sum(1 for r in results if r.get("ok"))
    times = [r["ms"] for r in results if r.get("ok")]
    return {
        "chunk_kib": chunk_bytes // 1024,
        "chunks": len(chunks),
        "per_chunk_ms": round(per_chunk_ms, 1),
        "sequential_mib_s": round(seq_mib_s, 3),
        "complete": complete,
        "clients": CLIENTS,
        "median_ok_s": round(statistics.median(times) / 1000, 1) if times else None,
        "mean_attempts_per_client": round(
            sum(r["attempts"] for r in results) / max(1, len(results)), 1),
    }


def main() -> int:
    # Deterministic, compressible-free payload so hashing is stable across sizes.
    data = os.urandom(OBJECT)
    out = {"object_bytes": OBJECT, "clients": CLIENTS, "concurrency": CONC,
           "retries": RETRIES, "rows": []}
    for kib in SIZES:
        row = run_size(kib * 1024, data)
        out["rows"].append(row)
        print(json.dumps(row), flush=True)

    # Pick the winner: must complete for every client, then lowest median time.
    full = [r for r in out["rows"] if r["complete"] == CLIENTS and r["median_ok_s"]]
    out["best"] = min(full, key=lambda r: r["median_ok_s"])["chunk_kib"] if full else None
    out["summary"] = [
        {"chunk_kib": r["chunk_kib"], "complete": r["complete"],
         "median_ok_s": r["median_ok_s"], "seq_mib_s": r["sequential_mib_s"]}
        for r in out["rows"]]
    print(json.dumps({k: out[k] for k in ("best", "summary")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
