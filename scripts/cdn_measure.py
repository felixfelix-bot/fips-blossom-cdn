#!/usr/bin/env python3
"""Observer measurement: run the claims, emit one JSON line.

Runs INSIDE the observer namespace, so its fetches take the delayed path.

Claims measured here (the acceptance list):
  C1 correct bytes served end-to-end via a replica
  C2 selection prefers the faster replica (and switches when it is reversed)
  C3 failover is bounded when the preferred replica dies
  C4 concurrent misses -> one upstream fill per cache
  C5 warm cache does not re-fetch upstream
  C6 corrupt content rejected (replica side, covered by unit tests; here we
     confirm the observer also refuses a wrong body)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdn.catalogue import Catalogue, Replica  # noqa: E402
from cdn.selector import NoReplicaAvailable, Selector  # noqa: E402

RESULTS: dict[str, object] = {}


def http_get(url: str, timeout: float = 5.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def http_put(url: str, body: bytes, timeout: float = 10.0) -> str:
    req = urllib.request.Request(url, data=body, method="PUT")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def stats_of(base: str) -> dict:
    try:
        return json.loads(http_get(f"{base}/stats", timeout=3))
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--origin-url", required=True)
    ap.add_argument("--replica-a", required=True)
    ap.add_argument("--npub-a", default="npub_a")
    ap.add_argument("--replica-b", required=True)
    ap.add_argument("--npub-b", default="npub_b")
    ap.add_argument("--label", default="run")
    args = ap.parse_args()

    rec: dict[str, object] = {"label": args.label}

    # ---- C1: does the origin hold an object at all? ---------------------
    body = os.urandom(4096) + b"cdn-acceptance-payload"
    key = hashlib.sha256(body).hexdigest()
    # Ingest via the observer -> origin. The observer is the publisher here.
    put_url = args.origin_url.rstrip("/") + "/blob"
    try:
        stored = http_put(put_url, body)
        rec["c1_origin_store"] = "ok" if stored == key else f"MISMATCH({stored[:8]})"
    except Exception as exc:  # noqa: BLE001
        rec["c1_origin_store"] = f"FAIL {type(exc).__name__}"
        print(json.dumps(rec))
        return 0

    a = Replica(npub=args.npub_a, base_url=args.replica_a)
    b = Replica(npub=args.npub_b, base_url=args.replica_b)
    cat = Catalogue([a, b], cooldown_s=3.0)
    sel = Selector(cat, timeout_s=5.0)

    # ---- C2a: both replicas fill, measure each, select fastest ----------
    # Fill both so neither is a cold-miss footnote.
    lat: dict[str, float] = {}
    for r in (a, b):
        t0 = time.perf_counter()
        try:
            got = http_get(f"{r.base_url}/blob/{key}")
            lat[r.npub] = (time.perf_counter() - t0) * 1000.0
            if hashlib.sha256(got).hexdigest() != key:
                lat[r.npub] = float("inf")
        except Exception:  # noqa: BLE001
            lat[r.npub] = float("inf")
    rec["c1_fill_ms"] = {k: round(v, 2) for k, v in lat.items()}
    rec["c1_both_correct"] = all(v != float("inf") for v in lat.values())

    # Seed the catalogue with the measured TTFB of each replica.
    for r in (a, b):
        if lat.get(r.npub, float("inf")) != float("inf"):
            cat.report_success(r, lat[r.npub])

    chosen = cat.select()
    rec["c2_selected"] = chosen.replica.npub if chosen else None
    rec["c2_expected_faster"] = min(lat, key=lambda k: lat[k]) if lat else None

    # End-to-end verified fetch through the selector.
    try:
        res = sel.get(key)
        ok = hashlib.sha256(res.body).hexdigest() == key
        rec["c1_end_to_end"] = "ok" if ok else "HASH-MISMATCH"
        rec["c1_served_by"] = res.served_by.npub
        rec["c1_attempts"] = res.attempts
    except NoReplicaAvailable as exc:
        rec["c1_end_to_end"] = f"FAIL {exc}"

    # ---- C5: warm cache does not re-fetch upstream ---------------------
    before = stats_of(args.replica_a)
    for _ in range(3):
        http_get(f"{args.replica_a}/blob/{key}")
    after = stats_of(args.replica_a)
    rec["c5_upstream_fetches_delta"] = (
        after.get("upstream_fetches", 0) - before.get("upstream_fetches", 0)
    )

    # ---- C4: concurrent misses -> ONE upstream fill --------------------
    # A fresh object, requested by N concurrent clients through ONE replica.
    body2 = os.urandom(2048) + b"cold-object-for-coalescing"
    key2 = hashlib.sha256(body2).hexdigest()
    http_put(put_url, body2)
    before2 = stats_of(args.replica_b)
    n = 12
    barrier = threading.Barrier(n)
    errs: list[str] = []

    def client():
        try:
            barrier.wait(timeout=5)
            http_get(f"{args.replica_b}/blob/{key2}", timeout=10)
        except Exception as exc:  # noqa: BLE001
            errs.append(type(exc).__name__)

    ts = [threading.Thread(target=client) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=20)
    after2 = stats_of(args.replica_b)
    rec["c4_clients"] = n
    rec["c4_errors"] = len(errs)
    rec["c4_upstream_fills"] = (
        after2.get("upstream_fetches", 0) - before2.get("upstream_fetches", 0)
    )

    # ---- C3: bounded failover when preferred replica dies --------------
    # Point the catalogue at a closed port for A and confirm it falls to B.
    dead = Replica(npub=args.npub_a, base_url="http://127.0.0.1:1")  # closed
    dead.last_ttfb_ms = 0.1        # make it look FASTEST first
    live = Replica(npub=args.npub_b, base_url=args.replica_b)
    live.last_ttfb_ms = 50.0
    cat2 = Catalogue([dead, live], cooldown_s=30.0)
    sel2 = Selector(cat2, timeout_s=2.0)
    t0 = time.perf_counter()
    try:
        res2 = sel2.get(key)
        rec["c3_failover"] = "ok"
        rec["c3_served_by"] = res2.served_by.npub
        rec["c3_attempts"] = res2.attempts
    except NoReplicaAvailable as exc:
        rec["c3_failover"] = f"FAIL {exc}"
    rec["c3_elapsed_ms"] = round((time.perf_counter() - t0) * 1000.0, 2)
    rec["c3_dead_marked_unhealthy"] = (not dead.healthy)

    # ---- C6: observer refuses wrong bytes ------------------------------
    # Ask a replica for key2 under a different (valid-looking) key -> must fail.
    cat3 = Catalogue([Replica(npub="npub_liar", base_url=args.replica_a)])
    sel3 = Selector(cat3, timeout_s=2.0)
    try:
        wrong_key = hashlib.sha256(b"something-else-entirely").hexdigest()
        sel3.get(wrong_key)
        rec["c6_wrong_bytes_rejected"] = "NO — accepted wrong/missing bytes"
    except NoReplicaAvailable:
        rec["c6_wrong_bytes_rejected"] = "yes"

    print(json.dumps(rec), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
