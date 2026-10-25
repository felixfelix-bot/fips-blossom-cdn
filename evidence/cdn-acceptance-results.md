# CDN acceptance results — cache, coalescing, replica selection

**Run:** 2026-10-26, local namespaces on this host. **This is not a fleet test and
not a FIPS test.** It answers a narrower and prior question: does the CDN logic
itself work, independent of the mesh transport?

## Topology

Real namespaces, real veth pairs, real `netem`:

```
origin ns ──┬── observer ns ── replica A ns
            └──                 replica B ns
```

- **Origin** stores objects (`PUT /blob`, keyed by SHA-256) and serves them.
- **Replica A / B** are independent caches, each with its own identity
  (`npub_replica_a`, `npub_replica_b`) and its own direct uplink to the origin.
- **Observer** is a *distinct* identity from both replicas, with a path to each.
- **Asymmetry:** `netem delay 60ms` on the **observer side** of one replica link,
  i.e. it models the viewer's path, not the replica's local speed.

Why the observer must be distinct: two same-identity daemons peering *with each
other* (the earlier withdrawn experiment) can never show which replica an
observer picks. Nothing about that topology asks the question.

Why the direct replica→origin links exist: the observer namespace is not a
router. Without them a replica-side cache miss fails `Network is unreachable` and
every fetch returns 502 — which is easy to misread as a cache bug. Found and
fixed during development.

## Result — phase 1 (A fast, B delayed)

```json
{"label":"phase1_A_fast","c1_origin_store":"ok",
 "c1_fill_ms":{"npub_replica_a":81.5,"npub_replica_b":219.99},
 "c1_both_correct":true,
 "c2_selected":"npub_replica_a","c2_expected_faster":"npub_replica_a",
 "c1_end_to_end":"ok","c1_served_by":"npub_replica_a","c1_attempts":1,
 "c5_upstream_fetches_delta":0,
 "c4_clients":12,"c4_errors":0,"c4_upstream_fills":1,
 "c3_failover":"ok","c3_served_by":"npub_replica_b","c3_attempts":2,
 "c3_elapsed_ms":123.08,"c3_dead_marked_unhealthy":true,
 "c6_wrong_bytes_rejected":"yes"}
```

## Result — phase 2 (delay MOVED to A)

```json
{"label":"phase2_reversed","c1_origin_store":"ok",
 "c1_fill_ms":{"npub_replica_a":125.54,"npub_replica_b":3.81},
 "c1_both_correct":true,
 "c2_selected":"npub_replica_b","c2_expected_faster":"npub_replica_b",
 "c1_end_to_end":"ok","c1_served_by":"npub_replica_b","c1_attempts":1,
 "c5_upstream_fetches_delta":0,
 "c4_clients":12,"c4_errors":0,"c4_upstream_fills":1,
 "c3_failover":"ok","c3_served_by":"npub_replica_b","c3_attempts":2,
 "c3_elapsed_ms":2.86,"c3_dead_marked_unhealthy":true,
 "c6_wrong_bytes_rejected":"yes"}
```

## Claims and verdicts

| # | Claim | Evidence | Verdict |
|---|---|---|---|
| C1 | Correct bytes served end-to-end through a replica | SHA-256 match, `c1_both_correct=true`, `c1_end_to_end=ok` | **PASS** |
| C2 | Selection prefers the faster replica | A: selected a (81.5 vs 220.0 ms). Reversed: selected b (3.8 vs 125.5 ms) | **PASS** |
| C3 | Failover is bounded; dead replica sidelined | `attempts=2`, `dead_marked_unhealthy=true`, 123 ms / 2.9 ms | **PASS** |
| C4 | Concurrent misses collapse to one upstream fill | 12 clients → **1** fill, 0 errors, both phases | **PASS** |
| C5 | Warm cache does not re-fetch upstream | `upstream_fetches_delta=0` over 3 warm reads | **PASS** |
| C6 | Corrupt / unmatched content rejected | wrong key refused; unit tests cover corrupt-upstream-not-cached | **PASS** |

**The reversal is the load-bearing result.** A single run where A is fast only
shows A won. The observer switching to B when the delay moved is what
distinguishes measurement-based selection from "first entry in the list won".

## Unit tests

13 tests, `python3 -m unittest discover -s tests`, all green. Covers hash
mismatch never cached, LRU byte-budget eviction, coalescing under a 16-thread
barrier, cooldown after failure, and unmeasured replicas ranking below measured.

**Honesty note:** these tests passed on their first run — they were written
alongside the implementation, not strictly RED-first. The acceptance harness is
what provides independent evidence, and it did fail three times before passing
(host firewall DROP on the origin; missing replica→origin route; a
catalogue/selector method error), each of which is recorded above.

## Limits — what this does NOT show

- **Not a CDN in production.** One host, loopback-speed veth, ~1 object per
  phase. No sustained throughput, no storage pressure, no eviction under real
  load, no concurrent publishers.
- **Not over FIPS.** The mesh data plane is a separate, unresolved problem. This
  proves the CDN logic is *transport-independent*; it does not prove it runs over
  the mesh. That separation is deliberate: it means the CDN design does not
  depend on fixing the FIPS bug first.
- **No authorisation.** Hash verification proves the bytes match the name, not
  that the requester was allowed to have them.
- **No range requests.** Whole-object only; no 206, no resume.
- **Latency model is synthetic.** `netem` delay is not a geographic path.

## Reproduce

```bash
sudo bash scripts/cdn_acceptance.sh        # writes evidence/cdn-acceptance.jsonl
python3 -m unittest discover -s tests -v   # 13 unit tests
```
