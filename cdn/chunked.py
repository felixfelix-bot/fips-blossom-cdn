"""Chunked objects with per-chunk verification and bounded concurrency.

Why this exists
---------------
Measured on the fleet (DE origin -> US replica, one 256 KiB object):

    2 concurrent flows   -> 2/2 complete
    4                    -> 4/4
    6                    -> 6/6
    8                    -> 6/8
    12                   -> 6/12
    16                   -> 6/16   (identical across three runs)

Exactly six concurrent bulk flows succeed and the rest deliver ZERO bytes. The
failures are bimodal: either the whole body arrives or nothing does. A single
whole-object fetch over a contended path therefore fails outright, and a
retry of the SAME object re-rolls the same dice at the same size.

Chunking fixes this for two reasons:

1. **Retry granularity.** A failed chunk costs one chunk, not the whole object.
   A fresh connection re-rolls the dice cheaply.
2. **Concurrency control.** Fetching with at most 4 workers stays under the
   measured ceiling of 6, instead of stacking 16 flows on a path that can carry
   six.

Each chunk is content-addressed and verified independently, so a lying or
truncating peer can corrupt at most one chunk and it is rejected before use.
The manifest itself is just another blob, so it travels through the existing
store and needs no new server endpoint.
"""
from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

# Measured on the fleet (512 KiB object, 16 concurrent clients, retry enabled):
#   16 KiB -> 38.0 s   64 KiB -> 38.0 s   256 KiB -> 20.1 s   512 KiB -> 17.6 s
# Round-trip cost dominates, so larger chunks win even above the flow ceiling,
# because retry -- not small chunks -- is what recovers a stalled flow.
DEFAULT_CHUNK = 256 * 1024
DEFAULT_CONCURRENCY = 4
DEFAULT_RETRIES = 4


class ChunkError(RuntimeError):
    """A chunk could not be fetched and verified within the retry budget."""


def split(data: bytes, chunk_size: int = DEFAULT_CHUNK) -> tuple[bytes, list[bytes]]:
    """Split `data` into chunks; return (manifest_bytes, chunks).

    The manifest is deterministic JSON with the chunk hashes in order, so the
    same input always produces the same manifest bytes (and thus the same blob
    address), which is what makes it content-addressable like everything else.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    chunks = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]
    if not chunks:                      # empty object still needs one entry
        chunks = [b""]
    manifest = {
        "v": 1,
        "size": len(data),
        "chunk_size": chunk_size,
        "chunks": [hashlib.sha256(c).hexdigest() for c in chunks],
    }
    return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(), chunks


def parse_manifest(blob: bytes) -> dict:
    m = json.loads(blob)
    if m.get("v") != 1:
        raise ValueError(f"unsupported manifest version: {m.get('v')!r}")
    if not isinstance(m.get("chunks"), list) or not m["chunks"]:
        raise ValueError("manifest has no chunks")
    return m


class ChunkedFetcher:
    """Fetch objects as chunks, with bounded concurrency and per-chunk retry.

    `fetch(hash_hex) -> bytes` is the caller's transport (fleet node, BlobRouter,
    anything). This class owns verification, retry and ordering only.
    """

    def __init__(self, fetch, *, concurrency: int = DEFAULT_CONCURRENCY,
                 retries: int = DEFAULT_RETRIES, backoff: float = 0.15):
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if retries < 1:
            raise ValueError("retries must be >= 1")
        self._fetch = fetch
        self._concurrency = concurrency
        self._retries = retries
        self._backoff = backoff
        self._lock = threading.Lock()
        self._inflight = 0
        self.peak_inflight = 0
        self.chunk_attempts = 0

    def _get_chunk(self, digest: str) -> bytes:
        last: Exception | None = None
        for attempt in range(self._retries):
            with self._lock:
                self._inflight += 1
                self.peak_inflight = max(self.peak_inflight, self._inflight)
                self.chunk_attempts += 1
            try:
                body = self._fetch(digest)
                actual = hashlib.sha256(body).hexdigest()
                if actual != digest:
                    # Retrying is right: a truncated body over a stalling path
                    # looks exactly like a lie until you compare the hash.
                    raise ChunkError(f"{digest}: got bytes hashing to {actual}")
                return body
            except Exception as exc:  # noqa: BLE001
                last = exc
                if attempt + 1 < self._retries:
                    threading.Event().wait(self._backoff * (2 ** attempt))
            finally:
                with self._lock:
                    self._inflight -= 1
        raise ChunkError(f"chunk {digest} failed after {self._retries} attempts: {last}")

    def fetch_hashes(self, digests, size: int) -> bytes:
        """Fetch a known list of chunk hashes and assemble exactly `size` bytes.

        Shared by the flat-manifest path and the paged-index path so both get the
        same bounded concurrency and per-chunk retry.
        """
        hexes = [d.hex() if isinstance(d, (bytes, bytearray)) else d for d in digests]
        if not hexes:
            raise ChunkError("no chunk hashes to fetch")
        # ThreadPoolExecutor is only used when there is more than one chunk.
        if len(hexes) == 1:
            parts = [self._get_chunk(hexes[0])]
        else:
            with ThreadPoolExecutor(max_workers=self._concurrency) as pool:
                parts = list(pool.map(self._get_chunk, hexes))
        body = b"".join(parts)
        if len(body) != size:
            raise ChunkError(f"assembled {len(body)} bytes, expected {size}")
        return body

    def fetch_manifest(self, manifest_bytes: bytes) -> bytes:
        m = parse_manifest(manifest_bytes)
        return self.fetch_hashes(m["chunks"], m["size"])


def fetch_blob_with_retry(fetch, digest: str, *, retries: int = 5,
                          backoff: float = 0.15) -> bytes:
    """Fetch one verified blob, retrying transient stalls.

    This exists because the flat manifest was previously fetched once, whole and
    unretried, which made it a single point of failure on a path where a flow can
    deliver zero bytes. Every entry point into stored bytes now retries.
    """
    last: Exception | None = None
    for attempt in range(retries):
        try:
            body = fetch(digest)
            actual = hashlib.sha256(body).hexdigest()
            if actual != digest:
                raise ChunkError(f"{digest}: got bytes hashing to {actual}")
            return body
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt + 1 < retries:
                threading.Event().wait(backoff * (2 ** attempt))
    raise ChunkError(f"{digest} failed after {retries} attempts: {last}")
