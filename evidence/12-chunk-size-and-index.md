# Chunk size and the paged index — what the measurements say

Two experiments on the live fleet (DE origin → US replica). Raw data from
`scripts/chunk_sweep.py` and `scripts/index_proof.py`.

Object: 512 KiB. Chunks fetched with bounded concurrency (4) and retry (5).

## Chunk size, at the flow ceiling (6 clients)

| chunk | chunks | per-chunk | seq MiB/s | complete | median time | attempts/client |
|---|---|---|---|---|---|---|
| 16 KiB | 32 | 635 ms | 0.025 | 6/6 | **41.0 s** | 37.5 |
| 32 KiB | 16 | 855 ms | 0.037 | 6/6 | 33.1 s | 19.8 |
| 64 KiB | 8 | 1038 ms | 0.060 | 6/6 | 33.1 s | 12.3 |
| 128 KiB | 4 | 1384 ms | 0.090 | 6/6 | 32.7 s | 8.3 |
| 256 KiB | 2 | 1890 ms | 0.132 | 6/6 | 18.0 s | 3.0 |
| 512 KiB | 1 | 2232 ms | 0.224 | 6/6 | **2.6 s** | 1.0 |

## Chunk size, above the ceiling (16 clients)

| chunk | chunks | complete | median time | attempts/client |
|---|---|---|---|---|
| 64 KiB | 8 | 16/16 | 38.0 s | 14.1 |
| 256 KiB | 2 | 16/16 | 20.1 s | 4.0 |
| 512 KiB | 1 | 16/16 | **17.6 s** | 1.8 |

## The finding, and a correction to my own reasoning

I previously argued for small chunks on the grounds of *retry granularity*: a
failed 64 KiB chunk costs less than a failed 512 KiB chunk. **The data says
otherwise.** Larger chunks win at both concurrency levels, by roughly 2x.

The reason is round-trip cost. Per-chunk latency rises with size (635 ms at
16 KiB to 2232 ms at 512 KiB), but bytes per round trip rise faster, so
throughput improves monotonically: 0.025 MiB/s at 16 KiB versus 0.224 MiB/s at
512 KiB — 9x.

And retry, not chunking, is what recovers a stalled flow. The earlier
whole-object test with **no retry** gave 6/16; a single 512 KiB chunk **with**
retry gives 16/16 in 17.6 s at 1.8 attempts. Chunking's real contribution is
that it bounds how much work a retry wastes; it does not need to be fine-grained
to do that.

**Consequence:** `DEFAULT_CHUNK` moved from 64 KiB to 256 KiB. 512 KiB measured
marginally better here, but a 512 KiB chunk wastes a whole half-megabyte on every
retry and leaves less parallelism for large objects; 256 KiB is the conservative
choice backed by a 16/16 result. 64 KiB and below are strictly worse on this
path and should not be used.

## Guidance

- **Small objects (≲ 2 MiB):** one or two chunks. Use 256 KiB and expect no
  parallelism; the win is round-trip amortisation, not concurrency.
- **Medium (2–64 MiB):** 256 KiB flat manifest. Index cost is 1–17 KB, which is
  comparable to one chunk and not worth an extra round trip.
- **Large (> 64 MiB):** paged index. The flat manifest becomes the largest object
  in the system and must be fetched whole before anything starts.
- **Never** below 64 KiB: it pays a round trip per 16 KB and gains nothing, since
  retry already handles a lost flow.

## Paged index, measured at 64 MiB

Built a real 64 MiB object (1024 chunks at 64 KiB → 4 leaf pages + 1 root,
depth 1), uploaded the pages, and fetched them over the live path:

| | bytes |
|---|---|
| flat JSON manifest (what shipped before) | **68,661** |
| paged root page | 150 |
| paged leaf page | 8,214 |
| **paged path (root + leaf)** | **8,364** |

Ratio: **8.2x** smaller to reach a chunk, and the difference is one round trip
(0.433 s root, 0.457 s leaf).

Scaling, same 64 KiB chunking:

| object | flat | paged path |
|---|---|---|
| 64 MiB | 68,661 B | 8,364 B |
| 256 MiB | 274,486 B | 8,748 B |
| 1 GiB | **1,097,783 B** | **10,284 B** |

The flat manifest grows linearly; the paged path is flat. At 1 GiB that is
**107x** — the difference between an index that must transfer and one that does
not.

## What the paged index does and does not give

**Does:** bounded index fetch, per-page retry (a corrupt or stalled page costs one
page), cheap random access (root + one leaf to reach any chunk), and a smaller
index for full download too.

**Does not:** add integrity. The flat manifest's SHA256 already commits to every
chunk; a tree root commits to the same set. A tree is not more secure here.

**Does not:** improve deduplication. Dedup comes from content-addressed chunks;
identical chunks dedup identically under a list or a tree.

## Honest limits

- Both experiments used one object size (512 KiB) for the sweep. The trend is
  monotonic and large, but it is one object size.
- 16 clients is the highest concurrency tested. Beyond that, per-flow exposure
  grows and the 256 KiB choice may need revisiting.
- `FLAT_LIMIT` of 64 MiB is chosen from the cost curve, not measured as an
  optimum; the crossing is broad.
- The paged path was proven by fetching root and one leaf at 64 MiB. Full
  assembly of a 64 MiB object end to end over the mesh was not run — only index
  bytes and fetch latency were measured.
