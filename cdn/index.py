"""Paged index over content-addressed chunks — a bounded-cost manifest.

Why a tree at all
-----------------
Our chunking shipped a flat JSON manifest: one blob listing every chunk hash.
It is compact for small objects and it already commits to every chunk, because
SHA256(manifest) pins the whole list. A tree buys **no extra integrity**.

What it buys is a bounded index fetch. Measured sizes at 64 KiB chunks:

    object    chunks   flat JSON   tree path
    256 KiB        4       319 B       176 B
    6 MiB         96     6,484 B     3,856 B
    64 MiB     1,024    68,661 B    10,432 B
    256 MiB    4,096   274,486 B    10,912 B
    1 GiB     16,384 1,097,783 B    12,832 B

Past ~64 MiB the flat manifest becomes the largest object in the system and it
must be fetched whole, once, before any chunk can start. That reintroduces
exactly the all-or-nothing failure chunking was meant to remove: one flow that
delivers zero bytes and the whole object is unreachable.

The tree makes the index O(log n) instead of O(n) and lets any page be retried
independently.

Format
------
Binary, not hex-in-JSON: hashes are raw 32-byte digests, which halves the index
for free.

    page := MAGIC(5) depth(u8) count(u16) fanout(u16) chunk_size(u32)
            total_size(u64) then count * 32-byte sha256

- depth 0 pages hold chunk hashes.
- depth d>0 pages hold the hashes of depth d-1 pages.
- The object's address is SHA256(root page bytes), so the root commits to
  everything below it. There is no separate manifest to trust.

Fanout 256 gives depth 2 up to 65,536 chunks = 4 GiB at 64 KiB, with a fetch
path of ~10 KB regardless of object size.
"""
from __future__ import annotations

import hashlib
import struct

from cdn.chunked import ChunkError, ChunkedFetcher, fetch_blob_with_retry

MAGIC = b"HTIX1"
HDR = struct.Struct(">5sBHH IQ".replace(" ", ""))
FANOUT = 256
# Below this, one manifest fetch is cheaper than an extra round trip per level.
FLAT_LIMIT = 64 * 1024 * 1024


def encode_page(depth: int, count: int, fanout: int, chunk_size: int,
                total_size: int, hashes: list[bytes]) -> bytes:
    if len(hashes) != count:
        raise ValueError(f"count={count} but {len(hashes)} hashes")
    if any(len(h) != 32 for h in hashes):
        raise ValueError("hashes must be 32 raw bytes")
    return HDR.pack(MAGIC, depth, count, fanout, chunk_size, total_size) + b"".join(hashes)


def decode_page(blob: bytes) -> dict:
    if len(blob) < HDR.size:
        raise ValueError("page shorter than header")
    magic, depth, count, fanout, chunk_size, total_size = HDR.unpack_from(blob)
    if magic != MAGIC:
        raise ValueError(f"bad page magic {magic!r}")
    body = blob[HDR.size:]
    if len(body) != count * 32:
        raise ValueError(f"page declares {count} hashes but carries {len(body)} bytes")
    hashes = [body[i * 32:(i + 1) * 32] for i in range(count)]
    return {"depth": depth, "count": count, "fanout": fanout,
            "chunk_size": chunk_size, "total_size": total_size, "hashes": hashes}


def build_index(data: bytes, chunk_size: int = 64 * 1024,
                fanout: int = FANOUT) -> tuple[list[bytes], list[bytes], str]:
    """Return (chunks, pages, root_hex).

    `pages` is ordered leaf-first; the last element is the root whose SHA256 is
    the object's address.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    chunks = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)] or [b""]

    pages: list[bytes] = []
    level = [hashlib.sha256(c).digest() for c in chunks]
    depth = 0
    while True:
        groups = [level[i:i + fanout] for i in range(0, len(level), fanout)]
        start = len(pages)
        for g in groups:
            pages.append(encode_page(depth, len(g), fanout, chunk_size, len(data), g))
        if len(groups) == 1:
            break                       # this page is the root
        # Hash the pages just written and promote them a level.
        level = [hashlib.sha256(pages[start + i]).digest() for i in range(len(groups))]
        depth += 1

    root = pages[-1]
    return chunks, pages, hashlib.sha256(root).hexdigest()


def choose_layout(size: int, flat_limit: int = FLAT_LIMIT) -> str:
    """'flat' or 'paged'. Trees cost a round trip; flat costs index bytes."""
    return "flat" if size <= flat_limit else "paged"


class IndexFetcher:
    """Fetch a paged object by root hash, retrying every page and every chunk."""

    def __init__(self, fetch, *, concurrency: int = 4, retries: int = 5,
                 backoff: float = 0.15):
        self._fetch = fetch
        self._retries = retries
        self._backoff = backoff
        self._chunks = ChunkedFetcher(fetch, concurrency=concurrency,
                                      retries=retries, backoff=backoff)

    def _page(self, digest_hex: str) -> dict:
        # A page is just a blob: same verify-then-retry discipline as a chunk.
        return decode_page(fetch_blob_with_retry(
            self._fetch, digest_hex, retries=self._retries, backoff=self._backoff))

    def fetch_object(self, root_hex: str) -> bytes:
        root = self._page(root_hex)
        if root["depth"] == 0:
            body = self._chunks.fetch_hashes(root["hashes"], root["total_size"])
            return body

        # Walk down one level at a time; each page is fetched with retry.
        level = [h.hex() for h in root["hashes"]]
        depth = root["depth"]
        while depth > 0:
            pages = [self._page(h) for h in level]
            if depth == 1:
                hashes = [h for p in pages for h in p["hashes"]]
                return self._chunks.fetch_hashes(hashes, root["total_size"])
            nxt: list[str] = []
            for p in pages:
                nxt.extend(h.hex() for h in p["hashes"])
            level = nxt
            depth -= 1
        raise ChunkError("index walk ended without reaching leaves")
