# Chunk size × concurrency — the full matrix

Run 2026-10-26 on the live fleet. DE origin, US replica, 1 MiB object,
4 concurrent fetches per client, 6 retries, per-chunk verification.
Raw data: `evidence/sweep-matrix.json`. Chart: `docs/sweep.svg`.

Chunks were seeded **locally on the replica** from a seeded SHAKE-256 stream, so
the client recomputes identical hashes and no setup traffic crosses the mesh.
Every number below is delivery only.

## Results

Median completion time, and clients completing out of clients attempted.

| chunk | 6 clients | 16 clients | 32 clients |
|---|---|---|---|
| 16 KiB | 32.6 s 6/6 | 80.1 s **12/16** | 82.0 s **2/32** |
| 32 KiB | 28.8 s 6/6 | 65.0 s 16/16 | 81.2 s **22/32** |
| 64 KiB | 32.8 s 6/6 | 48.5 s 16/16 | 65.7 s **27/32** |
| 128 KiB | 20.3 s 6/6 | 49.3 s 16/16 | **50.6 s 32/32** |
| 256 KiB | 33.7 s 6/6 | 50.0 s 16/16 | 75.1 s 32/32 |
| 512 KiB | 21.7 s 6/6 | 25.3 s 16/16 | 56.1 s 32/32 |
| 1024 KiB | **11.4 s** 6/6 | **18.6 s** 16/16 | 57.2 s 32/32 |

## What the matrix says

**1. Small chunks lose clients, not just time.** Below 128 KiB, high-concurrency
runs do not finish. 16 KiB at 32 clients completes for **2 of 32**. A timeout is
a failure, not a slow success, so this dominates any latency comparison — and it
is the opposite of what a "smaller chunks are safer" intuition predicts.

**2. The optimum moves with concurrency.**
- 6 clients: 1024 KiB, 11.4 s
- 16 clients: 1024 KiB, 18.6 s
- 32 clients: **128 KiB**, 50.6 s

At low concurrency one big transfer amortises the ~600 ms round trip. At high
contention a single large flow is expensive to retry, and spreading the work over
more flows wins. This is the real tradeoff: **round-trip amortisation versus
contention resilience** — not latency versus speed.

**3. No setting wins everywhere.** 512 KiB is the best compromise: 100%
completion at all three levels and within ~15% of the winner at each. It is the
default in `cdn/chunked.py`.

**4. Retry, not chunking, is the fix.** The whole-object fetch with no retry
delivered complete bytes to 6 of 16 clients. One 1 MiB chunk *with* retry
delivers 16/16 in 18.6 s at 1.1 attempts per client. Chunking bounds what a retry
wastes; it does not by itself repair anything.

## Retry cost

`mean_attempts` per client (from the raw JSON) falls sharply with chunk size,
which is why big chunks win on waste as well as time:

| chunk | attempts @6 | @16 | @32 |
|---|---|---|---|
| 16 KiB | 69.5 | 77.4 | 70.9 |
| 64 KiB | 19.8 | 23.2 | 27.1 |
| 256 KiB | 7.8 | 10.5 | 13.6 |
| 1024 KiB | 1.0 | 1.1 | 1.3 |

A per-flow stall is roughly size-independent, so retrying one large flow costs
far less in total round trips than retrying many small ones.

## Limits of this experiment

- One object size (1 MiB). Chunk sizes above the object size degenerate to a
  single chunk, so the 1024 KiB column is "whole object", not "chunking".
- Three concurrency levels (6, 16, 32). The crossover between 16 and 32 clients
  is bracketed, not located.
- One path (DE↔US) and one direction. The LAN behaves very differently — 15/16
  at 16 clients with no tuning.
- `median_ok_s` is taken over successful clients only, so a row that completes
  2/32 reports the time of those 2. Read it together with the completion column,
  never alone.
