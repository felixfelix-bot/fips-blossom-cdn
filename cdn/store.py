"""Content-addressed store on disk with an LRU byte budget.

Distinct from the in-memory cache: this is what a *replica* persists. Bytes are
verified against the requested hash BEFORE they are written, so a corrupt
upstream response can never poison the store.
"""
from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict


class Store:
    def __init__(self, root: str, budget_bytes: int):
        self.root = root
        self.budget = int(budget_bytes)
        self._lock = threading.Lock()
        self._index: "OrderedDict[str, int]" = OrderedDict()
        self._bytes = 0
        self.hits = 0
        self.misses = 0
        os.makedirs(root, exist_ok=True)
        self._load()

    def _path(self, key: str) -> str:
        return os.path.join(self.root, key[:2], key)

    def _load(self) -> None:
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for fn in filenames:
                if len(fn) != 64:
                    continue
                p = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(p)
                except OSError:
                    continue
                self._index[fn] = size
                self._bytes += size
        while self._bytes > self.budget and self._index:
            self._evict_one()

    def _evict_one(self) -> None:
        key, size = self._index.popitem(last=False)
        try:
            os.unlink(self._path(key))
        except OSError:
            pass
        self._bytes -= size

    def has(self, key: str) -> bool:
        with self._lock:
            return key in self._index

    def get(self, key: str) -> bytes:
        with self._lock:
            if key not in self._index:
                self.misses += 1
                raise KeyError(key)
            self._index.move_to_end(key)
            self.hits += 1
            path = self._path(key)
        with open(path, "rb") as fh:
            data = fh.read()
        if hashlib.sha256(data).hexdigest() != key:
            # Disk corruption or a partial write: drop it rather than serve it.
            self.delete(key)
            raise KeyError(f"{key} failed on-read verification")
        return data

    def put(self, key: str, data: bytes) -> None:
        got = hashlib.sha256(data).hexdigest()
        if got != key:
            raise ValueError(f"refusing to store: hash {got[:12]} != key {key[:12]}")
        path = self._path(key)
        with self._lock:
            if key in self._index:
                self._index.move_to_end(key)
                return
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            self._index[key] = len(data)
            self._bytes += len(data)
            while self._bytes > self.budget and len(self._index) > 1:
                self._evict_one()

    def delete(self, key: str) -> None:
        with self._lock:
            size = self._index.pop(key, None)
            if size is not None:
                self._bytes -= size
            try:
                os.unlink(self._path(key))
            except OSError:
                pass

    def stats(self) -> dict:
        with self._lock:
            return {
                "entries": len(self._index),
                "bytes": self._bytes,
                "budget": self.budget,
                "hits": self.hits,
                "misses": self.misses,
            }
