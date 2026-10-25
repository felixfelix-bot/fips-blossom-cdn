#!/usr/bin/env python3
"""Cache + singleflight coalescing for a content-addressed edge.

This is the piece that makes a CDN different from a pass-through proxy:

  * **Cache** keyed by content hash. Immutable objects, so no TTL is needed for
    correctness — only eviction pressure. Bounded by count and bytes, LRU.
  * **Coalescing** ("singleflight"): N simultaneous misses for one hash cause
    ONE upstream fetch. Everybody waits on that one, then reads from cache.

The coalescing rule that matters and is easy to get wrong: a naive coalescer
that makes every waiter read from a single streaming socket is head-of-line
blocking — one slow reader stalls the rest. This implementation instead waits
for the fill to *complete* into the cache, then each caller reads its own copy.
Memory is bounded because bytes land in the cache exactly once, not per waiter.

Verified-before-store: the upstream body is hashed and refused unless it matches
the requested key. On a mesh the upstream may be a stranger, so verification
replaces TLS trust.
"""
from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable


class HashMismatch(Exception):
    """Upstream returned bytes that do not hash to the requested key."""


@dataclass
class Stats:
    hits: int = 0
    misses: int = 0
    upstream_fetches: int = 0
    coalesced_waiters: int = 0
    evictions: int = 0
    stored_bytes: int = 0
    rejected_mismatch: int = 0

    def as_dict(self) -> dict:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "upstream_fetches": self.upstream_fetches,
            "coalesced_waiters": self.coalesced_waiters,
            "evictions": self.evictions,
            "stored_bytes": self.stored_bytes,
            "rejected_mismatch": self.rejected_mismatch,
        }


@dataclass
class _Fill:
    """One in-progress upstream fetch that other callers can wait on."""
    done: threading.Event = field(default_factory=threading.Event)
    error: BaseException | None = None


class EdgeCache:
    """Thread-safe content-addressed cache with singleflight fills."""

    def __init__(
        self,
        fetch: Callable[[str], bytes],
        *,
        max_entries: int = 1024,
        max_bytes: int = 64 * 1024 * 1024,
        verify: bool = True,
    ) -> None:
        self._fetch = fetch
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._verify = verify

        self._lock = threading.Lock()
        self._objects: OrderedDict[str, bytes] = OrderedDict()
        self._inflight: dict[str, _Fill] = {}
        self._bytes = 0
        self.stats = Stats()

    # ---- public API -----------------------------------------------------
    def get(self, sha256: str) -> bytes:
        """Return the object for `sha256`, filling from upstream on a miss.

        Concurrent callers for the same hash share ONE upstream fetch.
        """
        with self._lock:
            hit = self._objects.get(sha256)
            if hit is not None:
                self._objects.move_to_end(sha256)  # LRU touch
                self.stats.hits += 1
                return hit

            self.stats.misses += 1
            fill = self._inflight.get(sha256)
            if fill is not None:
                # Someone is already fetching this — wait for them.
                self.stats.coalesced_waiters += 1
                leader = False
            else:
                fill = _Fill()
                self._inflight[sha256] = fill
                leader = True

        if not leader:
            fill.done.wait()
            if fill.error is not None:
                raise fill.error
            with self._lock:
                cached = self._objects.get(sha256)
                if cached is None:
                    # Leader stored then evicted immediately (tiny cache).
                    raise HashMismatch(f"{sha256}: filled but not retained")
                self._objects.move_to_end(sha256)
                self.stats.hits += 1
                return cached

        # Leader: exactly one upstream fetch for this hash.
        try:
            with self._lock:
                self.stats.upstream_fetches += 1
            body = self._fetch(sha256)
            if self._verify:
                actual = hashlib.sha256(body).hexdigest()
                if actual != sha256:
                    with self._lock:
                        self.stats.rejected_mismatch += 1
                    raise HashMismatch(
                        f"{sha256}: upstream bytes hash to {actual}"
                    )
            self._store(sha256, body)
            return body
        except BaseException as exc:
            fill.error = exc
            raise
        finally:
            with self._lock:
                self._inflight.pop(sha256, None)
            fill.done.set()

    def has(self, sha256: str) -> bool:
        with self._lock:
            return sha256 in self._objects

    def __len__(self) -> int:
        with self._lock:
            return len(self._objects)

    def nbytes(self) -> int:
        with self._lock:
            return self._bytes

    # ---- internals ------------------------------------------------------
    def _store(self, sha256: str, body: bytes) -> None:
        size = len(body)
        with self._lock:
            if size > self._max_bytes:
                # Cannot ever fit: serve it, do not cache it.
                return
            if sha256 in self._objects:
                self._objects.move_to_end(sha256)
                return
            self._objects[sha256] = body
            self._bytes += size
            # Evict LRU until both bounds hold.
            while self._objects and (
                len(self._objects) > self._max_entries or self._bytes > self._max_bytes
            ):
                _, old = self._objects.popitem(last=False)
                self._bytes -= len(old)
                self.stats.evictions += 1
            self.stats.stored_bytes = self._bytes
