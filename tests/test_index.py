"""Paged index: codec, tree shape, retry, and the layout threshold."""
from __future__ import annotations

import hashlib
import threading
import unittest

from cdn.chunked import ChunkError
from cdn.index import (FANOUT, IndexFetcher, build_index, choose_layout,
                       decode_page, encode_page)


class Store:
    """blob transport: hex hash -> bytes, with optional fault injection."""

    def __init__(self, fail_first=0, corrupt=()):
        self.blobs = {}
        self._fail = fail_first
        self._corrupt = set(corrupt)
        self._lock = threading.Lock()
        self.calls = 0
        self.bytes_served = 0

    def put(self, body: bytes) -> str:
        h = hashlib.sha256(body).hexdigest()
        self.blobs[h] = body
        return h

    def __call__(self, digest: str) -> bytes:
        with self._lock:
            self.calls += 1
            fail = self._fail > 0
        if fail:
            with self._lock:
                self._fail -= 1
            raise TimeoutError("simulated stall")
        body = self.blobs[digest]
        with self._lock:
            self.bytes_served += len(body)
        if digest in self._corrupt:
            return body[:-1] + b"X"
        return body


class TestPageCodec(unittest.TestCase):
    def test_roundtrip(self):
        hs = [hashlib.sha256(bytes([i])).digest() for i in range(5)]
        blob = encode_page(depth=1, count=5, fanout=FANOUT, chunk_size=65536,
                           total_size=1234, hashes=hs)
        p = decode_page(blob)
        self.assertEqual(p["depth"], 1)
        self.assertEqual(p["hashes"], hs)
        self.assertEqual(p["total_size"], 1234)

    def test_rejects_bad_magic(self):
        blob = bytearray(encode_page(0, 1, FANOUT, 65536, 10,
                                     [b"\0" * 32]))
        blob[0:5] = b"NOPE!"
        with self.assertRaises(ValueError):
            decode_page(bytes(blob))

    def test_rejects_truncated_body(self):
        blob = encode_page(0, 3, FANOUT, 65536, 10, [b"\1" * 32] * 3)
        with self.assertRaises(ValueError):
            decode_page(blob[:-1])


class TestTreeShape(unittest.TestCase):
    def test_single_page_is_root_at_depth_zero(self):
        data = b"a" * (64 * 1024 * 2)
        chunks, pages, root = build_index(data, 64 * 1024)
        self.assertEqual(len(pages), 1)
        self.assertEqual(decode_page(pages[0])["depth"], 0)
        self.assertEqual(root, hashlib.sha256(pages[0]).hexdigest())

    def test_depth_two_when_chunks_exceed_fanout(self):
        n = FANOUT + 1
        data = bytes(n * 1000)
        chunks, pages, root = build_index(data, chunk_size=1000)
        self.assertEqual(len(chunks), n)
        depths = [decode_page(p)["depth"] for p in pages]
        self.assertIn(0, depths)
        self.assertEqual(max(depths), 1)          # one internal level above leaves
        self.assertEqual(decode_page(pages[-1])["depth"], 1)

    def test_root_commits_to_everything(self):
        a, pa, ra = build_index(b"x" * 5000, 1000)
        b, pb, rb = build_index(b"x" * 4999 + b"y", 1000)
        self.assertNotEqual(ra, rb, "different content must give a different root")


class TestIndexFetcher(unittest.TestCase):
    def _store(self, data, chunk_size=64 * 1024):
        chunks, pages, root = build_index(data, chunk_size)
        s = Store()
        for c in chunks:
            s.put(c)
        for p in pages:
            s.put(p)
        return s, root

    def test_depth_one_roundtrip(self):
        data = bytes(range(256)) * 900
        s, root = self._store(data)
        self.assertEqual(IndexFetcher(s).fetch_object(root), data)

    def test_depth_two_roundtrip(self):
        data = bytes(range(256)) * 3000       # 768000 -> 12 chunks at 64 KiB
        s, root = self._store(data, chunk_size=64 * 1024)
        self.assertEqual(IndexFetcher(s, concurrency=4).fetch_object(root), data)

    def test_retries_a_failing_page(self):
        data = b"z" * 200000
        s, root = self._store(data)
        s._fail = 2                            # first two fetches stall
        got = IndexFetcher(s, retries=5, backoff=0.01).fetch_object(root)
        self.assertEqual(got, data)

    def test_rejects_corrupt_page(self):
        data = b"q" * 200000
        chunks, pages, root = build_index(data, 64 * 1024)
        s = Store()
        for c in chunks:
            s.put(c)
        # corrupt the stored root page bytes themselves
        s.blobs[hashlib.sha256(pages[-1]).hexdigest()] = pages[-1][:-1] + b"X"
        # Caught by hash verification, so it surfaces as ChunkError, not a parse
        # error: the bytes are rejected before they are ever decoded.
        with self.assertRaises(ChunkError):
            IndexFetcher(s, retries=2, backoff=0.01).fetch_object(root)

    def test_index_bytes_are_bounded_for_large_objects(self):
        """Root alone must stay small regardless of object size."""
        big_chunks = FANOUT * 4
        data = bytes(big_chunks * 64)
        _, pages, root = build_index(data, chunk_size=64)
        root_page = next(p for p in pages
                         if hashlib.sha256(p).hexdigest() == root)
        self.assertLessEqual(len(root_page), 22 + FANOUT * 32)


class TestLayoutThreshold(unittest.TestCase):
    def test_small_objects_use_flat(self):
        self.assertEqual(choose_layout(1024 * 1024), "flat")

    def test_large_objects_use_paged(self):
        self.assertEqual(choose_layout(256 * 1024 * 1024), "paged")

    def test_threshold_boundary(self):
        self.assertEqual(choose_layout(64 * 1024 * 1024), "flat")
        self.assertEqual(choose_layout(64 * 1024 * 1024 + 1), "paged")


if __name__ == "__main__":
    unittest.main()
