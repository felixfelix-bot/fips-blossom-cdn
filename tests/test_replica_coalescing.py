"""Coalescing must not turn one slow fill into N client failures.

Field evidence (fleet run 2026-10-26, evidence/10-fleet-claims.md): 16 concurrent
misses against a US replica produced exactly 1 upstream fill and 15 coalesced
waiters -- the mechanism worked -- but 10 of the 16 clients failed.

Cause: UrlUpstream defaults to a 20 s timeout. Over the transatlantic mesh a
256 KiB fill runs at ~3.4 kB/s, i.e. ~77 s, so the leader's fetch times out and
every waiter inherits that error. Coalescing multiplied one slow fetch into
sixteen failed requests instead of deduplicating the work.

These tests pin both halves: a slow-but-succeeding fill must serve every waiter,
and a fill that genuinely fails must fail every waiter (no silent partial).
"""
from __future__ import annotations

import hashlib
import threading
import time
import unittest

from cdn.replica import ReplicaCache
from cdn.store import Store
from cdn.upstream import UpstreamError


class SlowUpstream:
    """Serves the key after `delay` seconds, counting fetches."""

    def __init__(self, body: bytes, delay: float = 0.0, fail: bool = False):
        self.body = body
        self.delay = delay
        self.fail = fail
        self.fetches = 0
        self._lock = threading.Lock()

    def fetch(self, key: str) -> bytes:
        with self._lock:
            self.fetches += 1
        time.sleep(self.delay)
        if self.fail:
            raise UpstreamError("upstream said no")
        return self.body


class TestCoalescingUnderSlowFill(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.store = Store(self.tmp, budget_bytes=8 * 1024 * 1024)

    def _fanout(self, cache, key, n=16):
        errs, oks = [], []
        barrier = threading.Barrier(n)

        def client():
            try:
                barrier.wait(timeout=20)
                body, _hit = cache.get(key)
                oks.append(body)
            except Exception as exc:  # noqa: BLE001
                errs.append(exc)

        ts = [threading.Thread(target=client) for _ in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=60)
        return oks, errs

    def test_slow_fill_serves_every_waiter(self):
        """The fill is slow but succeeds; all 16 clients must get the bytes."""
        body = b"slow-object" * 4096
        key = hashlib.sha256(body).hexdigest()
        up = SlowUpstream(body, delay=1.5)
        cache = ReplicaCache(up, self.store)

        oks, errs = self._fanout(cache, key)
        self.assertEqual([str(e) for e in errs], [],
                         "a successful fill must not fail any waiter")
        self.assertEqual(len(oks), 16)
        self.assertTrue(all(o == body for o in oks))
        self.assertEqual(up.fetches, 1, "exactly one upstream fetch")

    def test_failed_fill_fails_every_waiter(self):
        """A genuine upstream failure must not be hidden from some waiters."""
        body = b"will-fail" * 1024
        key = hashlib.sha256(body).hexdigest()
        up = SlowUpstream(body, delay=0.2, fail=True)
        cache = ReplicaCache(up, self.store)

        oks, errs = self._fanout(cache, key)
        self.assertEqual(len(oks), 0)
        self.assertEqual(len(errs), 16)


if __name__ == "__main__":
    unittest.main()
