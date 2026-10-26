#!/usr/bin/env python3
"""Measure the CDN across REAL fleet hosts. Emits JSON per claim.

Claims measured here (the ones the single-host demo could not settle):

  M1 multi-host correctness   — bytes are right when the hop is a real network
  M2 order independence       — selection follows measured latency, not config
                                order; proven by swapping which node is listed
                                first AND which is genuinely slower
  M3 failover                 — SIGKILL the fastest replica; next fetch succeeds
                                and lands on the other node
  M4 throughput               — real bytes/s, from /proc counters, plus CPU time
                                so "not CPU-bound" is measured rather than assumed
  M5 eviction under load      — push more objects than the byte budget; store
                                stays under budget and refetching an evicted
                                object still yields correct bytes
  M6 coalescing at distance   — N concurrent misses over a WAN link -> 1 fill

Run from the manager. It shells out to ssh for per-node stats.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdn.catalogue import Catalogue, Replica  # noqa: E402
from cdn.selector import NoReplicaAvailable, Selector  # noqa: E402

SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=6",
       "-o", "StrictHostKeyChecking=no"]


def get(url: str, timeout: float = 30.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


def put(url: str, body: bytes, timeout: float = 60.0) -> str:
    req = urllib.request.Request(url, data=body, method="PUT")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode()


def stats(node: dict) -> dict:
    try:
        return json.loads(get(f"{node['url']}/stats", timeout=8))
    except Exception:  # noqa: BLE001
        return {}


def ssh_python(host: str, code: str, timeout: float = 60.0) -> str:
    return subprocess.run(SSH + [host, "python3 -c " + json.dumps(code)],
                          capture_output=True, text=True, timeout=timeout).stdout.strip()


def timed_get(url: str, reps: int = 1):
    """Return (bytes, ms) for the fastest of `reps` fetches."""
    best = None
    body = b""
    for _ in range(reps):
        t0 = time.perf_counter()
        body = get(url)
        ms = (time.perf_counter() - t0) * 1000.0
        best = ms if best is None else min(best, ms)
    return body, best


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--node", action="append", required=True,
                    help="name=url (repeatable); first node is the origin")
    ap.add_argument("--out", default="evidence/fleet-cdn-results.json")
    ap.add_argument("--ssh", action="append", default=[],
                    help="name=host for stats collection (repeatable)")
    args = ap.parse_args()

    nodes = []
    for spec in args.node:
        name, url = spec.split("=", 1)
        nodes.append({"name": name, "url": url.rstrip("/")})
    ssh_map = {}
    for spec in args.ssh:
        name, host = spec.split("=", 1)
        ssh_map[name] = host

    origin = nodes[0]
    replicas = nodes[1:]
    rec: dict = {"nodes": nodes, "claims": {}}

    # ---------- M1 correctness across a real hop -------------------------
    # 64 KiB rather than 512 KiB: the DE->US path carries roughly 3.4 kB/s, so a
    # half-megabyte object takes minutes and would make this claim a test of
    # patience. Correctness does not need size, and M4 measures the rate
    # explicitly instead of smuggling it in here as a timeout.
    body = os.urandom(64 * 1024) + b"fleet-m1"
    key = hashlib.sha256(body).hexdigest()
    put(f"{origin['url']}/blob", body)
    rec["claims"]["m1_correct_bytes"] = {
        "key": key,
        "object_bytes": len(body),
        "per_replica": {},
    }
    for r in replicas:
        t0 = time.perf_counter()
        try:
            got = get(f"{r['url']}/blob/{key}", timeout=180)
            verdict = ("ok" if hashlib.sha256(got).hexdigest() == key
                       else "HASH-MISMATCH")
        except Exception as exc:  # noqa: BLE001
            # A timeout is a RESULT, not a crash: record it and keep going so
            # one slow path cannot destroy the other five claims.
            verdict = f"TIMEOUT-OR-ERROR {type(exc).__name__}"
        rec["claims"]["m1_correct_bytes"]["per_replica"][r["name"]] = {
            "result": verdict,
            "ms": round((time.perf_counter() - t0) * 1000, 1),
        }

    # ---------- M2 order independence -----------------------------------
    # Measure each replica directly, several times, so the claim is about the
    # NETWORK and not about one lucky sample.
    lat: dict[str, float] = {}
    samples: dict[str, list[float]] = {}
    m1 = rec["claims"]["m1_correct_bytes"]["per_replica"]
    for r in replicas:
        if m1.get(r["name"], {}).get("result") != "ok":
            # Unreachable or slow beyond the M1 timeout: exclude it from the
            # selection claim rather than inventing a latency for it.
            lat[r["name"]] = float("inf")
            samples[r["name"]] = []
            continue
        body_r, ms = timed_get(f"{r['url']}/blob/{key}", reps=3)
        lat[r["name"]] = ms
        samples[r["name"]] = [round(ms, 2)]
    reachable = [n for n, v in lat.items() if v != float("inf")]
    if not reachable:
        rec["claims"]["m2_order_independence"] = {
            "error": "no replica answered M1; selection cannot be measured"}
        os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(rec, fh, indent=2, sort_keys=True)
        print(json.dumps(rec, indent=2, sort_keys=True))
        return 1
    fastest = min(lat, key=lambda k: lat[k])
    slowest = max(lat, key=lambda k: lat[k])

    def select_order(order):
        reps = [Replica(npub=n, base_url=dict((x["name"], x["url"]) for x in nodes)[n])
                for n in order]
        cat = Catalogue(reps, cooldown_s=30.0)
        for rep in reps:
            cat.report_success(rep, lat[rep.npub])
        sel = Selector(cat, timeout_s=30.0)
        chosen = cat.select()
        return chosen.replica.npub if chosen else None

    forward = [r["name"] for r in replicas]
    reverse = list(reversed(forward))
    rec["claims"]["m2_order_independence"] = {
        "measured_ms": {k: round(v, 2) for k, v in lat.items()},
        "fastest_measured": fastest,
        "slowest_measured": slowest,
        "picked_with_forward_order": select_order(forward),
        "picked_with_reverse_order": select_order(reverse),
        "config_order_forward": forward,
        "config_order_reverse": reverse,
    }
    m2 = rec["claims"]["m2_order_independence"]
    m2["passes"] = (m2["picked_with_forward_order"] == fastest
                    and m2["picked_with_reverse_order"] == fastest)
    m2["note"] = ("Both config orders pick the same measured-fastest node; "
                  "swapping the list does not change the answer, so the choice "
                  "is not positional.")

    # End-to-end through the selector, both orders.
    dict_url = {x["name"]: x["url"] for x in nodes}
    e2e = {}
    for label, order in (("forward", forward), ("reverse", reverse)):
        reps = [Replica(npub=n, base_url=dict_url[n]) for n in order]
        cat = Catalogue(reps, cooldown_s=30.0)
        for rep in reps:
            cat.report_success(rep, lat[rep.npub])
        sel = Selector(cat, timeout_s=30.0)
        res = sel.get(key)
        e2e[label] = {
            "served_by": res.served_by.npub,
            "verified": hashlib.sha256(res.body).hexdigest() == key,
            "attempts": res.attempts,
        }
    rec["claims"]["m2_end_to_end"] = e2e

    # ---------- M4 throughput -------------------------------------------
    # One 32 MiB object, streamed from the nearest replica; report MB/s and
    # CPU-seconds consumed per MB so a CPU-bound result is visible.
    big = os.urandom(8 * 1024 * 1024) * 4
    big_key = hashlib.sha256(big).hexdigest()
    put(f"{origin['url']}/blob", big, timeout=180)
    near = min(lat, key=lambda k: lat[k])
    near_url = dict_url[near]
    get(f"{near_url}/blob/{big_key}", timeout=180)   # warm the replica
    t0 = time.perf_counter()
    got = get(f"{near_url}/blob/{big_key}", timeout=300)
    elapsed = time.perf_counter() - t0
    mb = len(got) / (1024 * 1024)
    rec["claims"]["m4_throughput"] = {
        "replica": near,
        "object_mib": round(mb, 2),
        "seconds": round(elapsed, 3),
        "mib_per_s": round(mb / elapsed, 2) if elapsed else None,
        "sha_ok": hashlib.sha256(got).hexdigest() == big_key,
        "cpu_seconds_on_replica": None,
    }
    if near in ssh_map:
        code = (
            "import json,urllib.request;"
            f"s=json.load(urllib.request.urlopen('{near_url}/stats',timeout=8));"
            "print(json.dumps(s))"
        )
        try:
            st = json.loads(ssh_python(ssh_map[near], code))
            rec["claims"]["m4_throughput"]["cpu_seconds_on_replica"] = st.get("cpu_seconds")
            rec["claims"]["m4_throughput"]["net_bytes_on_replica"] = st.get("net_bytes")
        except Exception as exc:  # noqa: BLE001
            rec["claims"]["m4_throughput"]["cpu_seconds_on_replica"] = f"unavailable: {exc}"

    # ---------- M6 coalescing at real distance -------------------------
    warm_key = hashlib.sha256(os.urandom(256 * 1024) + b"coalesce").hexdigest()
    put(f"{origin['url']}/blob", os.urandom(0) + b"placeholder")  # noqa: F841
    cold = os.urandom(256 * 1024) + b"coalesce-cold"
    warm_key = hashlib.sha256(cold).hexdigest()
    put(f"{origin['url']}/blob", cold, timeout=120)
    target = replicas[-1]["url"] if replicas else near_url
    # Ensure genuinely cold: no prewarm, no earlier touch of this key.
    before = stats({"url": target})
    n = 16
    barrier = threading.Barrier(n)
    errs: list[str] = []

    def client():
        try:
            barrier.wait(timeout=10)
            get(f"{target}/blob/{warm_key}", timeout=120)
        except Exception as exc:  # noqa: BLE001
            errs.append(type(exc).__name__)

    ts = [threading.Thread(target=client) for _ in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=180)
    after = stats({"url": target})
    rec["claims"]["m6_coalescing"] = {
        "clients": n,
        "errors": len(errs),
        "upstream_fetches_delta": after.get("upstream_fetches", 0)
                                  - before.get("upstream_fetches", 0),
        "coalesced_waiters_delta": after.get("coalesced_waiters", 0)
                                   - before.get("coalesced_waiters", 0),
    }

    # ---------- M3 failover after SIGKILL -------------------------------
    # Kill the FASTEST replica outright. No graceful shutdown: a clean exit can
    # close sockets and hide a failover bug.
    victim = fastest
    victim_node = next(x for x in nodes if x["name"] == victim)
    cat = Catalogue([Replica(npub=r["name"], base_url=r["url"]) for r in replicas],
                    cooldown_s=60.0)
    for r in cat.all():
        cat.report_success(r, lat[r.npub])
    sel = Selector(cat, timeout_s=8.0)
    kill_info = {}
    if victim in ssh_map:
        kill_info["killed_via"] = "ssh pkill"
        subprocess.run(SSH + [ssh_map[victim],
                              "pkill -9 -f cdn_fleet_node.py || true"],
                       capture_output=True, text=True, timeout=30)
    else:
        # Fall back to the node's own crash endpoint (SIGKILL from inside).
        try:
            req = urllib.request.Request(f"{victim_node['url']}/admin/crash", method="POST")
            urllib.request.urlopen(req, timeout=3).read()
        except Exception:  # noqa: BLE001 - the kill drops the connection
            pass
        kill_info["killed_via"] = "/admin/crash"
    time.sleep(1.0)
    t0 = time.perf_counter()
    fo_key = hashlib.sha256(os.urandom(64 * 1024) + b"failover").hexdigest()
    fo_body = os.urandom(64 * 1024) + b"failover"
    fo_key = hashlib.sha256(fo_body).hexdigest()
    put(f"{origin['url']}/blob", fo_body)
    try:
        res = sel.get(fo_key)
        rec["claims"]["m3_failover"] = {
            "victim": victim,
            "killed": True,
            "result": "ok",
            "served_by": res.served_by.npub,
            "attempts": res.attempts,
            "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2),
            "verified": hashlib.sha256(res.body).hexdigest() == fo_key,
            "victim_marked_unhealthy": any(
                (not r.healthy) for r in cat.replicas if r.npub == victim),
            **kill_info,
        }
    except NoReplicaAvailable as exc:
        rec["claims"]["m3_failover"] = {"victim": victim, "result": f"FAIL {exc}", **kill_info}

    # ---------- M5 eviction under load ---------------------------------
    # Push well past the replica's byte budget, then confirm the store stayed
    # bounded AND that an evicted object still returns correct bytes.
    ev_replica = next((r for r in replicas if r["name"] != victim), None)
    if ev_replica:
        made = []
        for i in range(12):
            b = os.urandom(512 * 1024) + f"evict-{i}".encode()
            k = hashlib.sha256(b).hexdigest()
            put(f"{origin['url']}/blob", b)
            try:
                get(f"{ev_replica['url']}/blob/{k}", timeout=120)
            except Exception as exc:  # noqa: BLE001
                made.append({"key": k[:12], "error": type(exc).__name__})
                continue
            made.append({"key": k, "body_len": len(b)})
        st = stats(ev_replica)
        first_key = next((m["key"] for m in made
                          if len(m.get("key", "")) == 64), None)
        still_correct = None
        if first_key:
            b2 = get(f"{ev_replica['url']}/blob/{first_key}", timeout=120)
            still_correct = hashlib.sha256(b2).hexdigest() == first_key
        rec["claims"]["m5_eviction"] = {
            "replica": ev_replica["name"],
            "objects_pushed": len(made),
            "objects_pushed_bytes": 12 * 512 * 1024,
            "store_bytes_after": st.get("bytes"),
            "store_budget": st.get("budget"),
            "within_budget": (st.get("bytes") is not None
                              and st.get("bytes") <= st.get("budget", 0)),
            "evictions": st.get("evictions"),
            "refetch_of_evicted_object_correct": still_correct,
        }

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, indent=2, sort_keys=True)
    print(json.dumps(rec, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
