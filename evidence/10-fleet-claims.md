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
  cause is not simply an overstated MTU. **Unresolved.**
- **CPU-boundedness** of the 11 MiB/s figure (no `--ssh` on this run).
- **Authorization.** Any node serves any key it holds.
- **Range/resume.** Not implemented.

One run is one sample. These are measurements, not yet a distribution.
