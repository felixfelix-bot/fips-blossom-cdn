#!/usr/bin/env python3
"""Unit tests: cache, coalescing, catalogue, selector.

Each test maps to a claim from the demo's acceptance list:
  C1 correct bytes served
  C2 selection prefers the faster replica
  C3 failover is bounded (and the dead replica is cooled down)
  C4 one upstream fill per cache under concurrency
  C5 warm cache does not re-fetch upstream
  C6 corrupt content is rejected and never cached
"""
import hashlib
import threading
import time
import unittest

from cdn.catalogue import Catalogue, Replica
from cdn.edgecache import EdgeCache, HashMismatch
from cdn.selector import NoReplicaAvailable, Selector


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class CountingFetcher:
    """Upstream that counts calls and can be told to be slow or wrong."""

    def __init__(self, objects: dict[str, bytes], delay_s: float = 0.0):
        self.objects = objects
        self.delay_s = delay_s
        self.calls = 0
        self.lock = threading.Lock()
        self.corrupt = set()

    def __call__(self, key: str) -> bytes:
        with self.lock:
            self.calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        if key in self.corrupt:
            return b"corrupted-payload"
        if key not in self.objects:
            raise KeyError(key)
        return self.objects[key]


# ---------------------------------------------------------------- C1, C4, C5
class TestEdgeCache(unittest.TestCase):
    def test_c1_returns_correct_bytes(self):
        body = b"hello-cdn" * 100
        f = CountingFetcher({sha(body): body})
        cache = EdgeCache(f)
        self.assertEqual(cache.get(sha(body)), body)

    def test_c4_concurrent_misses_cause_one_upstream_fetch(self):
        body = b"popular-object" * 1000
        f = CountingFetcher({sha(body): body}, delay_s=0.15)
        cache = EdgeCache(f)

        results, errors = [], []
        barrier = threading.Barrier(16)

        def worker():
            try:
                barrier.wait(timeout=5)   # release all threads together
                results.append(cache.get(sha(body)))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(len(results), 16)
        self.assertTrue(all(r == body for r in results))
        # THE claim: 16 viewers, ONE upstream fetch.
        self.assertEqual(f.calls, 1, f"expected 1 upstream fetch, got {f.calls}")
        self.assertEqual(cache.stats.upstream_fetches, 1)
        self.assertGreaterEqual(cache.stats.coalesced_waiters, 14)

    def test_c5_warm_cache_does_not_refetch(self):
        body = b"warm" * 10
        f = CountingFetcher({sha(body): body})
        cache = EdgeCache(f)
        for _ in range(5):
            self.assertEqual(cache.get(sha(body)), body)
        self.assertEqual(f.calls, 1)

    def test_c6_corrupt_upstream_is_rejected_and_not_cached(self):
        want = b"the-real-thing"
        key = sha(want)
        f = CountingFetcher({key: want})
        f.corrupt.add(key)              # upstream lies
        cache = EdgeCache(f)
        with self.assertRaises(HashMismatch):
            cache.get(key)
        self.assertFalse(cache.has(key), "corrupt body must never be cached")
        self.assertEqual(cache.stats.rejected_mismatch, 1)

    def test_eviction_respects_byte_budget(self):
        objs = {sha(bytes([i]) * 100): bytes([i]) * 100 for i in range(1, 6)}
        f = CountingFetcher(objs)
        cache = EdgeCache(f, max_bytes=250)   # room for 2 objects only
        for k in objs:
            cache.get(k)
        self.assertLessEqual(cache.nbytes(), 250)
        self.assertGreater(cache.stats.evictions, 0)


# --------------------------------------------------------------- C2, C3
class TestCatalogue(unittest.TestCase):
    def test_c2_prefers_fastest_measured_replica(self):
        slow = Replica(npub="npub_slow", base_url="http://slow")
        fast = Replica(npub="npub_fast", base_url="http://fast")
        slow.last_ttfb_ms = 200.0
        fast.last_ttfb_ms = 20.0
        cat = Catalogue([slow, fast])
        self.assertIs(cat.select().replica, fast)

    def test_unmeasured_replica_ranks_after_measured(self):
        measured = Replica(npub="npub_m", base_url="http://m")
        measured.last_ttfb_ms = 500.0   # slow, but at least measured
        unknown = Replica(npub="npub_u", base_url="http://u")
        cat = Catalogue([unknown, measured])
        order = [s.replica.key() for s in cat.candidates()]
        self.assertEqual(order[0], measured.key())

    def test_c3_failed_replica_enters_cooldown_and_is_skipped(self):
        a = Replica(npub="npub_a", base_url="http://a")
        b = Replica(npub="npub_b", base_url="http://b")
        cat = Catalogue([a, b], cooldown_s=10.0)
        cat.report_failure(a)
        avail = [r.key() for r in cat.available()]
        self.assertNotIn(a.key(), avail)
        self.assertIn(b.key(), avail)
        self.assertIs(cat.select().replica, b)

    def test_all_failed_means_no_selection(self):
        a = Replica(npub="npub_a", base_url="http://a")
        cat = Catalogue([a])
        cat.report_failure(a)
        self.assertIsNone(cat.select())


# --------------------------------------------------------------- C1/C2/C3/C6 e2e-ish
class FakeReplicaServer:
    """In-process replica stand-in with injectable behaviour."""

    def __init__(self, objects: dict[str, bytes], *, delay_s: float = 0.0,
                 serve_wrong: bool = False, fail: bool = False):
        self.objects = objects
        self.delay_s = delay_s
        self.serve_wrong = serve_wrong
        self.fail = fail
        self.calls = 0

    def fetch(self, base_url: str, key: str, timeout: float) -> bytes:
        self.calls += 1
        if self.delay_s:
            time.sleep(self.delay_s)
        if self.fail:
            raise ConnectionError("refused")
        if self.serve_wrong:
            return b"WRONG-BYTES"
        if key not in self.objects:
            raise KeyError(key)
        return self.objects[key]


class TestSelector(unittest.TestCase):
    def test_c1_verified_body_via_best_replica(self):
        body = b"cdn-payload" * 50
        key = sha(body)
        good = FakeReplicaServer({key: body})
        r = Replica(npub="npub_good", base_url="http://good")
        sel = Selector(Catalogue([r]), fetcher=good.fetch)
        res = sel.get(key)
        self.assertEqual(res.body, body)
        self.assertEqual(res.served_by.npub, "npub_good")
        self.assertEqual(res.attempts, 1)

    def test_c3_bounded_failover_to_second_replica(self):
        body = b"failover-body"
        key = sha(body)
        dead = FakeReplicaServer({}, fail=True)
        live = FakeReplicaServer({key: body})

        r_dead = Replica(npub="npub_dead", base_url="http://dead")
        r_live = Replica(npub="npub_live", base_url="http://live")

        # Order dead first by giving it a fake fast TTFB.
        r_dead.last_ttfb_ms = 1.0
        r_live.last_ttfb_ms = 2.0

        servers = {"http://dead": dead, "http://live": live}

        def fetch(base_url, k, timeout):
            return servers[base_url].fetch(base_url, k, timeout)

        sel = Selector(Catalogue([r_dead, r_live]), fetcher=fetch)
        res = sel.get(key)
        self.assertEqual(res.body, body)
        self.assertEqual(res.served_by.npub, "npub_live")
        self.assertEqual(res.attempts, 2)
        self.assertFalse(r_dead.healthy, "dead replica must be marked unhealthy")

    def test_c6_wrong_bytes_from_replica_are_rejected(self):
        body = b"correct"
        key = sha(body)
        liar = FakeReplicaServer({key: body}, serve_wrong=True)
        r = Replica(npub="npub_liar", base_url="http://liar")
        sel = Selector(Catalogue([r]), fetcher=liar.fetch)
        with self.assertRaises(NoReplicaAvailable):
            sel.get(key)

    def test_no_replicas_raises(self):
        sel = Selector(Catalogue([]), fetcher=lambda *a: b"")
        with self.assertRaises(NoReplicaAvailable):
            sel.get("0" * 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)
