"""Two-tier replica cache: memory (coalescing) behind disk (persistence).

Layer split, and what each one is for:

  * ``EdgeCache``  — the singleflight gate. Exactly one upstream fill per hash,
    no matter how many clients miss at once. Also the memory LRU fast path.
  * ``Store``      — the durable copy with its own byte budget, so a restart or a
    memory eviction does not turn into an upstream fetch.

Verification happens BEFORE the disk write, in this wrapper. That ordering is the
point: ``EdgeCache`` verifies too, but it verifies on the way into memory, which
is after our fetch returns. Storing first and verifying later would let a lying
upstream put bytes on disk under someone else's name.
"""
from __future__ import annotations

import hashlib

from cdn.edgecache import EdgeCache, HashMismatch
from cdn.store import Store
from cdn.upstream import UpstreamError


class ReplicaCache:
    def __init__(self, upstream, store: Store, *, memory_bytes: int = 64 * 1024 * 1024,
                 max_entries: int = 4096, entry_max_bytes: int = 8 * 1024 * 1024):
        self.upstream = upstream
        self.store = store
        self.entry_max_bytes = entry_max_bytes
        self._cache = EdgeCache(self._fetch_verified, max_entries=max_entries,
                                max_bytes=memory_bytes)

    def _fetch_verified(self, key: str) -> bytes:
        # Disk tier first: a persisted object is not an upstream fetch.
        if self.store.has(key):
            try:
                return self.store.get(key)
            except KeyError:
                pass  # corrupted on disk; fall through and refill
        body = self.upstream.fetch(key)
        actual = hashlib.sha256(body).hexdigest()
        if actual != key:
            raise HashMismatch(f"{key}: upstream bytes hash to {actual}")
        # Oversized bodies are served but never cached; that keeps the byte
        # budget meaningful instead of silently exceeded by one huge object.
        if len(body) <= self.entry_max_bytes:
            self.store.put(key, body)
        return body

    def get(self, key: str):
        """Return (body, was_hit)."""
        was_hit = self._cache.has(key) or self.store.has(key)
        try:
            return self._cache.get(key), was_hit
        except UpstreamError as exc:
            raise KeyError(str(exc)) from exc

    def stats(self) -> dict:
        memory = self._cache.stats.as_dict()
        disk = self.store.stats()
        return {
            "tier": "replica",
            "entries": disk["entries"],
            "bytes": disk["bytes"],
            "budget": disk["budget"],
            "memory_entries": len(self._cache),
            "memory_bytes": self._cache.nbytes(),
            "hits": memory["hits"],
            "misses": memory["misses"],
            "upstream_fetches": memory["upstream_fetches"],
            "coalesced_waiters": memory["coalesced_waiters"],
            "evictions": memory["evictions"],
            "rejected_mismatch": memory["rejected_mismatch"],
        }
