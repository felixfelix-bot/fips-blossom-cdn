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

### After fixing the MSS, M6 still fails — they are NOT one bug

The path-MTU problem was real and is now fixed (see the throughput section: the
same 512 KiB transfer went from 17.21 s to 2.13 s). The obvious inference was
that M6 would come with it. **It did not.** Re-running M6 after deploying the
MSS clamp to all four hosts:

- clients 16, **ok 6, fail 10**, `TimeoutError`
- 1 upstream fetch, 15 coalesced waiters — unchanged

So my earlier claim that M6 and the throughput gap were one phenomenon was
**wrong**, and this document previously said so. Corrected here.

A second capture, taken with the clamp active (SYN and SYN-ACK both advertise
`mss 1000`, confirmed in the trace), shows the stall has moved but not gone:

| delivered | connections |
|---|---|
| complete object | 5 |
| ~50,788 B | 2 |
| ~33,005 B | 3 |
| ~21,973 B | 2 |
| **~19,556 B** | 7 |

The consistent stall is now ~19,556 bytes. At the clamped segment size that is
about **19-20 packets**; before clamping the stall was ~20,764 bytes, roughly
**17-20 packets** at the old segment size. The bound looks like a packet count
rather than a byte count, which would point at a fixed reorder/congestion window
in the FIPS transport rather than at TCP segment sizing.

**That is a hypothesis, not a finding.** I have not measured it. What I can say
is that clamping MSS changed the single-stream rate by 8x and changed the
concurrent-client failure not at all.

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
- **Throughput at distance — FIXED.** Root cause was path MTU. `fips0` advertises
  MTU 1280, so TCP negotiates MSS ~1220 and emits ~1260-byte segments, but the
  real path drops anything above ~1198 bytes (`ping6 -s 1150` succeeds, `-s 1200`
  fails) while the inbound nft drop counter stays still. Full-size segments were
  silently discarded and retransmitted forever.

  Fix: clamp the advertised MSS on `fips0` egress (`table inet fips-mss`, unit
  `fips-mss.service`, in this repo's playbook). Measured on a 512 KiB transfer
  DE→US, same object, same hosts:

  | MSS | seconds |
  |---|---|
  | default (1220) | 17.21 |
  | 1000 | 3.11 |
  | 900 | 2.89 |
  | 1000, clamped at both ends via nft | **2.13** |

  The clamp has to exist on the receiving side, because an MSS option advertises
  what the sender can *receive*: clamping only the origin left the transfer at
  9.75 s, and adding it on the US host took it to 2.13 s.

  This does **not** fix M6. See above — I predicted it would and was wrong.
- **CPU-boundedness** of the 11 MiB/s figure (no `--ssh` on this run).
- **Authorization.** Any node serves any key it holds.
- **Range/resume.** Not implemented.

One run is one sample. These are measurements, not yet a distribution.
