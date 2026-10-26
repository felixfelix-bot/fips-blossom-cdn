"""Chunked transfer must verify, retry and stay under the concurrency ceiling."""
from __future__ import annotations

import hashlib
import threading
import unittest

from cdn.chunked import ChunkedFetcher, ChunkError, parse_manifest, split


class FakeTransport:
    """Serves from a dict of hash -> body, failing the first N times per hash."""

    def __init__(self, chunks, fail_first=0, corrupt_hashes=()):
        self._by_hash = {hashlib.sha256(c).hexdigest(): c for c in chunks}
        self._fail_first = dict.fromkeys(self._by_hash, fail_first)
        self._corrupt = set(corrupt_hashes)
        self._lock = threading.Lock()
        self.calls = []
        self.active = 0
        self.peak = 0

    def __call__(self, digest: str) -> bytes:
        with self._lock:
            self.calls.append(digest)
            self.active += 1
            self.peak = max(self.peak, self.active)
            remaining = self._fail_first.get(digest, 0)
            if remaining:
                self._fail_first[digest] = remaining - 1
        try:
            if remaining:
                raise TimeoutError("simulated stall")
            body = self._by_hash[digest]
            if digest in self._corrupt:
                return body[:-1] + b"X"      # wrong bytes, right length
            return body
        finally:
            with self._lock:
                self.active -= 1


class TestSplitAndManifest(unittest.TestCase):
    def test_roundtrip_preserves_bytes(self):
        data = bytes(range(256)) * 700        # 179200 bytes -> 3 chunks at 64 KiB
        manifest, chunks = split(data, chunk_size=64 * 1024)
        self.assertEqual(len(chunks), 3)
        self.assertEqual(b"".join(chunks), data)
        m = parse_manifest(manifest)
        self.assertEqual(m["size"], len(data))
        self.assertEqual(m["chunks"], [hashlib.sha256(c).hexdigest() for c in chunks])

    def test_manifest_is_deterministic(self):
        a, _ = split(b"x" * 1000)
        b, _ = split(b"x" * 1000)
        self.assertEqual(a, b, "same input must give the same manifest bytes")

    def test_empty_object_still_has_one_chunk(self):
        manifest, chunks = split(b"")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(parse_manifest(manifest)["size"], 0)

    def test_rejects_unknown_version(self):
        with self.assertRaises(ValueError):
            parse_manifest(b'{"v":99,"chunks":["a"],"size":1}')


class TestChunkedFetcher(unittest.TestCase):
    def test_fetches_and_verifies(self):
        data = b"payload" * 50000
        manifest, chunks = split(data)
        t = FakeTransport(chunks)
        f = ChunkedFetcher(t, concurrency=4)
        self.assertEqual(f.fetch_manifest(manifest), data)

    def test_retries_transient_stall(self):
        data = b"z" * 200000
        manifest, chunks = split(data)
        t = FakeTransport(chunks, fail_first=2)
        f = ChunkedFetcher(t, concurrency=4, retries=4, backoff=0.01)
        self.assertEqual(f.fetch_manifest(manifest), data)

    def test_gives_up_after_retry_budget(self):
        data = b"z" * 200000
        manifest, chunks = split(data)
        t = FakeTransport(chunks, fail_first=99)
        f = ChunkedFetcher(t, concurrency=2, retries=2, backoff=0.01)
        with self.assertRaises(ChunkError):
            f.fetch_manifest(manifest)

    def test_corrupt_chunk_is_rejected_not_used(self):
        data = b"q" * 150000
        manifest, chunks = split(data)
        bad = hashlib.sha256(chunks[1]).hexdigest()
        t = FakeTransport(chunks, corrupt_hashes={bad})
        f = ChunkedFetcher(t, concurrency=2, retries=2, backoff=0.01)
        with self.assertRaises(ChunkError):
            f.fetch_manifest(manifest)

    def test_never_exceeds_configured_concurrency(self):
        data = b"c" * (64 * 1024 * 8)
        manifest, chunks = split(data)
        t = FakeTransport(chunks)
        f = ChunkedFetcher(t, concurrency=4)
        f.fetch_manifest(manifest)
        self.assertLessEqual(t.peak, 4, f"peak concurrency {t.peak} exceeded 4")
        self.assertLessEqual(f.peak_inflight, 4)

    def test_stays_under_measured_flow_ceiling(self):
        """The fleet caps at 6 concurrent bulk flows; default must sit below it."""
        from cdn.chunked import DEFAULT_CONCURRENCY
        self.assertLess(DEFAULT_CONCURRENCY, 6,
                        "default concurrency must stay under the measured cap of 6")


if __name__ == "__main__":
    unittest.main()
