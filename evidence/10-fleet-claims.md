# Fleet CDN — measured claims

Run date: 2026-10-26. Raw data: `evidence/fleet-cdn-results.json`.

Executed **on `hz-de-a` (dq05)**, not on the manager: the manager's FIPS node has
no direct peer path to the US hosts, so running there measured the manager's
routing table rather than the CDN.

Topology: one origin (`hz-de-a`, DE) and three replicas — `hz-de-b` (DE, same
site), `hz-us-a` and `hz-us-b` (US). Two sites, not four: `testserver2` and
`vps2fips` are the same machine.

## Results

**M1 — correct bytes across a real hop: PASS**

| replica | result | ms |
|---|---|---|
| hz-de-b | ok | 328 |
| hz-us-a | ok | 2947 |
| hz-us-b | ok | 4391 |

SHA-256 verified on every body. This is the claim the namespace demo could not
make: the bytes crossed real links and arrived intact.

**M2 — selection follows measurement, not list order: PASS**

Measured latency: `hz-de-b` **14.68 ms**, `hz-us-b` **1026.86 ms**,
`hz-us-a` **1052.33 ms**.

Selection picked `hz-de-b` with the replica list in forward order **and** with
the list reversed. Reversing the configuration does not change the answer, so
the choice is not positional. End-to-end through the selector: served by
`hz-de-b`, **1 attempt**, bytes verified, in both orders.

A ~70x spread here, from geography alone. No `netem`.

**M3 — failover after SIGKILL: PASS**

`hz-de-b` (the fastest, and therefore the selector's first choice) was killed
via `/admin/crash` — `os._exit(9)`, no graceful shutdown. The next fetch:

- served by `hz-us-b`, **2 attempts**
- bytes verified
- `hz-de-b` marked unhealthy
- 3964 ms (dominated by the 1 s transatlantic latency)

Bounded failover, not a retry storm.

**M4 — throughput: PASS**

6 MiB object from the nearest replica: **0.546 s**, **11.0 MiB/s**, SHA verified.

`cpu_seconds_on_replica` is `null`: this run omitted `--ssh`, so per-node CPU
was not collected. **11 MiB/s is therefore a measured rate, not a proven
non-CPU-bound figure.** The single-host run reported CPU time; this one did not.

**M5 — eviction under real load: PASS**

24 × 512 KiB = **12 MiB** pushed to a replica with an **8 MiB** budget.

- evictions: **11**
- store after: **7,864,439 bytes** ≤ 8,388,608 budget
- refetch of an evicted object: **correct bytes**

The store stayed inside its budget and an evicted object came back intact, which
is what eviction is supposed to mean. The budget is small *by design* on this
node (`cdn_budget_bytes: 8388608` in the inventory) because exceeding a 256 MiB
budget would have measured the transatlantic link instead of the LRU.

**M6 — coalescing at distance: PARTIAL — mechanism works, delivery does not**

16 concurrent clients, all cold, against `hz-us-b`:

- upstream fetches: **1** ✓
- coalesced waiters: **15** ✓
- client errors: **10** ✗

The coalescing mechanism does exactly what it claims: sixteen simultaneous
misses produced one upstream fill and fifteen waiters. But ten of the sixteen
clients failed to receive the result. That is a real defect, not noise — the
waiters are given the coalesced fill but their requests do not survive it to
completion within the client window.

Recorded as partial. It is not a pass, and the mechanism working does not make
the user-visible behaviour correct.

### M6 follow-up: what it is not

Reproduced twice on the fleet with per-client exception types recorded
(`scripts/m6_probe.py`). The result is deterministic, not flaky:

| object size | clients | ok | fail | exception | upstream fetches | waiters |
|---|---|---|---|---|---|---|
| 256 KiB | 16 | 6 | 10 | `TimeoutError` | 1 | 15 |
| 32 KiB | 16 | 6 | 10 | `TimeoutError` | 1 | 15 |

Ruled out, each by measurement rather than argument:

- **Not the coalescer.** Unit tests show a slow-but-successful fill serves all
  16 waiters with exactly one fetch, and a failed fill fails all 16. The fleet
  numbers agree: 1 fetch, 15 waiters, in both runs.
- **Not object size or bandwidth.** 32 KiB and 256 KiB give the identical 6/10
  split. If egress bandwidth were the limit, an eight-fold smaller object would
  have changed the outcome.
- **Not connection capacity.** 16 parallel `/health` requests from the same
  client to the same node returned **16/16 HTTP 200**. The listener accepts
  concurrent connections fine.
- **Not the accept backlog.** `socketserver` defaults `request_queue_size` to 5,
  which looked like a good candidate, but a real 16-connection test against the
  server passes with backlog 5. That hypothesis was wrong and the test that
  "confirmed" it was deleted rather than kept as a false guard.
- **Not a lock held during the response write.** The blob handler calls
  `_send` with no lock held, so one slow reader cannot stall the others.

What remains is specific to holding several connections open for the duration
of a fill: requests that complete quickly (`/health`) all succeed, requests that
sit open across a multi-second fill fail in the majority. The leading untested
hypothesis is that the mesh drops long-idle TCP connections, which would need a
`tcpdump` on the replica during a concurrent fill to confirm. **Unconfirmed —
do not treat it as the answer.**

### M6 resolved as to mechanism: transfers stall after ~20 KB

A capture on the replica during a 16-client run (`scripts/pcap_conn_summary.py`)
settles it. Nothing is dropped and nothing is reset:

- client connections seen: **18**
- handshake completed: **18**
- received payload: **18**
- reset: **0**
- closed with FIN: **8**

Every connection was accepted and every one received bytes. The split is in how
many bytes arrived:

| delivered | connections | closed |
|---|---|---|
| **33,005 B (complete 32 KiB object)** | 7 | yes |
| ~23,181 B | 2 | no |
| ~20,764 B | 7 | no |
| 19,549 B | 1 | no |

Seven connections got the whole object and finished. Ten stalled at roughly
**20 KB** and never closed, so the client sat until its 300 s timeout.

So the earlier "idle connection" hypothesis is **wrong** too: the connections
are not idle and not dropped, they are mid-transfer and then simply stop
advancing. The stall point is a byte count (~20 KB), not a timeout and not an
object-size fraction — which is why shrinking the object from 256 KiB to 32 KiB
changed nothing but the amount left undelivered.

This is the same phenomenon as the "3.4 kB/s" figure in the throughput section:
that number was never steady-state bandwidth. It was a transfer that stalled and
was measured end to end. Both open issues are one issue.

Practical consequence: the M6 test as written conflates "does coalescing
deduplicate the work" with "can this mesh deliver to N concurrent clients over
the duration of a fill". Those are separate questions, and only the first is
answered.

## What this establishes

The CDN design works on real hardware across a real transatlantic path:
content addressing holds, selection tracks measured latency rather than
configuration order, failover is bounded, the store respects its budget, and
coalescing deduplicates.

## What it does not establish

- **M6 client delivery** — 10/16 concurrent clients failed.
- **Throughput at distance.** The same 512 KiB test ran at 4.28 MB/s DE↔DE and
  **3.4 kB/s** DE→US. `fips0` advertises MTU 1280; `ping6 -s 1150` (1198 bytes)
  succeeds and `-s 1200` (1248 bytes) fails, while the nft drop counter does not
  move during a stalled transfer. Full-size segments are being lost and the
  Packet-Too-Big signal is not reaching the sender. Lowering the interface MTU
  to 1100 made it worse, not better (the mesh address dropped entirely), so the
  cause is not simply an overstated MTU. **Unresolved** — but note the packet
  capture in the M6 section shows transfers stalling after ~20 KB, so 3.4 kB/s
  is a stalled transfer measured end to end, not a steady-state rate. The two
  open issues are one.
- **CPU-boundedness** of the 11 MiB/s figure (no `--ssh` on this run).
- **Authorization.** Any node serves any key it holds.
- **Range/resume.** Not implemented.

One run is one sample. These are measurements, not yet a distribution.
