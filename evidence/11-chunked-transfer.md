# Concurrent-transfer ceiling, and the chunked fix

Run: 2026-10-26, live fleet, DE origin → US replica, 256 KiB object, two sites.

## The ceiling

A whole-object fetch, N clients at once, same object, cached on the replica,
using `curl` (a C client, so this is not a Python artefact):

| concurrent clients | complete | zero bytes |
|---|---|---|
| 2 | 2 | 0 |
| 4 | 4 | 0 |
| 6 | 6 | 0 |
| 8 | 6 | 2 |
| 12 | 6 | 6 |
| 16 | 6 | 10 |

**Exactly six concurrent bulk flows succeed.** The failures are bimodal: the
body either arrives whole or not at all. Sixteen clients gave 6/16 on three
consecutive runs — deterministic, not flaky.

Same test on the **LAN** (DE host → DE host, sub-millisecond): **15/16**. So the
ceiling scales with path latency, not with concurrency in the abstract.

## What was ruled out, with the measurement

- **Not the Python client.** `curl` reproduces 6/16 exactly.
- **Not the coalescer.** The server reports 1 upstream fill and 15 waiters.
- **Not the node's firewall.** The nft drop counter does not move.
- **Not UDP receive-buffer overflow.** `Udp: … RcvbufErrors` on the replica did
  not increment across a 16-flow run.
- **Not TUN transmit drops.** `ip -s link show fips0` showed 0 TX drops and 0
  errors, while transmitting ~2 MB during the run.
- **Not object size.** 32 KiB and 256 KiB give the same split.
- **Not the accept backlog.** 16 parallel `/health` requests all return 200.

What is left is the FIPS daemon's own handling of concurrent flows. Recorded as
a finding with a bound, not a diagnosis: the effect is real, reproducible and
path-dependent, and it is not in our code.

## The fix

`cdn/chunked.py`: split objects into 64 KiB chunks, each content-addressed and
independently verified, with a deterministic JSON manifest (itself a blob).
`ChunkedFetcher` fetches with **bounded concurrency (default 4, below the
measured ceiling of 6) and per-chunk retry**.

Retry granularity is the point: a failed chunk costs one 64 KiB chunk and a
fresh connection, instead of losing a whole object to a flow that delivers zero.

### Measured result

16 concurrent clients, same object, same path, same hosts:

| | complete | failed |
|---|---|---|
| whole-object fetch | 6 | 10 |
| chunked fetch (4 workers, 5 retries) | **16** | **0** |

190 chunk attempts for a minimum of 80 — retries happen, as expected on this
path, and they converge. Peak in-flight per client never exceeded 4.

## Honest limits

- 64 KiB chunks on a path that can lose a whole flow means ~2.4x the requests.
  That is the price; it is bounded and predictable.
- This works around the ceiling. It does not raise it. Raising it needs the FIPS
  daemon side, which is not ours.
- The manifest is a single blob. For very large objects the manifest itself
  becomes a large fetch and would need its own chunking (a tree, as hashtree
  does) — not implemented here.
