"""Tests for the durable store and the two-tier replica cache."""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdn.edgecache import HashMismatch
from cdn.replica import ReplicaCache
from cdn.store import Store
from cdn.upstream import UpstreamError


def blob(n: int, salt: str = "") -> bytes:
    return (salt or "x").encode() * n


class TestStore(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.s = Store(self.dir, budget_bytes=10_000)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_put_and_get_roundtrip(self):
        b = blob(100, "a")
        k = hashlib.sha256(b).hexdigest()
        self.s.put(k, b)
        self.assertEqual(self.s.get(k), b)
        self.assertTrue(self.s.has(k))

    def test_refuses_hash_mismatch(self):
        b = blob(50)
        with self.assertRaises(ValueError):
            self.s.put("0" * 64, b)
        self.assertFalse(self.s.has("0" * 64))

    def test_evicts_lru_to_stay_under_budget(self):
        # Budget deliberately BELOW 10x100 so eviction must actually happen.
        # (An earlier version of this test used budget 1000 for 1000 bytes of
        # objects, asserted eviction, and was simply wrong: exactly-at-budget is
        # allowed. The test was fixed, not the store.)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        s = Store(d, budget_bytes=950)
        keys = []
        for i in range(10):
            b = blob(100, f"{i}")
            k = hashlib.sha256(b).hexdigest()
            s.put(k, b)
            keys.append(k)
        self.assertLessEqual(s.stats()["bytes"], 950)
        self.assertFalse(s.has(keys[0]))       # oldest evicted
        self.assertTrue(s.has(keys[-1]))       # newest retained

    def test_reload_indexes_existing_files(self):
        b = blob(120, "persist")
        k = hashlib.sha256(b).hexdigest()
        self.s.put(k, b)
        s2 = Store(self.dir, budget_bytes=10_000)
        self.assertTrue(s2.has(k))
        self.assertEqual(s2.get(k), b)

    def test_detects_on_disk_corruption(self):
        b = blob(80, "c")
        k = hashlib.sha256(b).hexdigest()
        self.s.put(k, b)
        path = self.s._path(k)          # deliberate: verify the guard fires
        with open(path, "wb") as fh:
            fh.write(blob(80, "z"))     # same length, different bytes
        with self.assertRaises(KeyError):
            self.s.get(k)


class TestReplicaCache(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(self.dir, budget_bytes=100_000)
        self.fetches = 0
        self.bodies: dict[str, bytes] = {}

    def make(self, fn=None):
        class U:
            def __init__(self, outer):
                self.outer = outer

            def fetch(self, k):
                self.outer.fetches += 1
                if fn:
                    return fn(k)
                return self.outer.bodies[k]

        return ReplicaCache(U(self), self.store)

    def test_disk_tier_avoids_second_upstream_fetch(self):
        b = blob(200, "disk")
        k = hashlib.sha256(b).hexdigest()
        self.bodies[k] = b
        rc = self.make()
        rc.get(k)
        self.assertEqual(self.fetches, 1)
        # New cache object, same store: must come from disk, not upstream.
        rc2 = self.make()
        body, hit = rc2.get(k)
        self.assertEqual(body, b)
        self.assertTrue(hit)
        self.assertEqual(self.fetches, 1, "disk tier should have served it")

    def test_lying_upstream_is_rejected_and_not_stored(self):
        k = hashlib.sha256(b"the-real-thing").hexdigest()
        rc = self.make(fn=lambda _k: b"an-impostor-body")
        with self.assertRaises(HashMismatch):
            rc.get(k)
        self.assertFalse(self.store.has(k), "impostor bytes must never reach disk")

    def test_upstream_error_surfaces_as_keyerror(self):
        def boom(_k):
            raise UpstreamError("origin down")
        rc = self.make(fn=boom)
        with self.assertRaises(KeyError):
            rc.get("a" * 64)

    def test_oversized_body_served_but_not_cached(self):
        b = blob(5000, "big")
        k = hashlib.sha256(b).hexdigest()
        self.bodies[k] = b
        rc = ReplicaCache(_Up(self), self.store, entry_max_bytes=1000)
        body, _hit = rc.get(k)
        self.assertEqual(body, b)
        self.assertFalse(self.store.has(k))

    def test_stats_expose_fills_and_hits(self):
        b = blob(150, "stats")
        k = hashlib.sha256(b).hexdigest()
        self.bodies[k] = b
        rc = self.make()
        rc.get(k)
        rc.get(k)
        st = rc.stats()
        self.assertEqual(st["upstream_fetches"], 1)
        self.assertGreaterEqual(st["hits"], 1)


class _Up:
    def __init__(self, outer):
        self.outer = outer

    def fetch(self, k):
        self.outer.fetches += 1
        return self.outer.bodies[k]


if __name__ == "__main__":
    unittest.main()
