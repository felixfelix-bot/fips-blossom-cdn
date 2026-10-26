#!/usr/bin/env python3
"""Prove the paged index is bounded while the flat manifest is O(n).

Builds a real 64 MiB object, uploads its index pages to a live replica, and
measures what it actually costs to reach a chunk. Compares against the flat JSON
manifest the previous implementation shipped, byte for byte.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cdn.index import build_index, choose_layout, decode_page  # noqa: E402

TARGET = "http://[fdfd:c0e5:3717:6cb1:bb60:de97:987e:7149]:18080"
MIB = 1024 * 1024


def put(base, body, timeout=180):
    req = urllib.request.Request(f"{base}/blob", data=body, method="PUT")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def put_retry(base, body, tries=8):
    last = None
    for _ in range(tries):
        try:
            return put(base, body)
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(0.3)
    raise RuntimeError(f"upload failed: {last}")


def get(base, key, timeout=120):
    with urllib.request.urlopen(f"{base}/blob/{key}", timeout=timeout) as r:
        return r.read()


def flat_manifest_bytes(size: int, chunk: int) -> int:
    """Exactly what cdn.chunked.split() emits today: hex hashes in JSON."""
    n = max(1, -(-size // chunk))
    return len(json.dumps({"v": 1, "size": size, "chunk_size": chunk,
                           "chunks": ["0" * 64] * n},
                          sort_keys=True, separators=(",", ":")).encode())


def main() -> int:
    size = 64 * MIB
    chunk = 64 * 1024
    # Deterministic and cheap to build at 64 MiB.
    data = (b"hashtree-gap-probe" * 4096)[:chunk] * (size // chunk)
    data += b"\0" * (size - len(data))

    t0 = time.perf_counter()
    chunks, pages, root = build_index(data, chunk)
    build_s = time.perf_counter() - t0

    depths = {}
    for p in pages:
        d = decode_page(p)["depth"]
        depths[d] = depths.get(d, 0) + 1

    root_page = pages[-1]
    leaf_pages = [p for p in pages if decode_page(p)["depth"] == 0]
    flat = flat_manifest_bytes(len(data), chunk)

    # Upload only the index pages: they are the object of interest here.
    up0 = time.perf_counter()
    for p in pages:
        put_retry(TARGET, p)
    upload_s = time.perf_counter() - up0

    # Time the real index fetch over the live path.
    t1 = time.perf_counter()
    got_root = get(TARGET, root)
    root_s = time.perf_counter() - t1
    assert got_root == root_page, "root bytes mismatch"

    leaf_key = decode_page(root_page)["hashes"][0].hex()
    t2 = time.perf_counter()
    got_leaf = get(TARGET, leaf_key)
    leaf_s = time.perf_counter() - t2

    out = {
        "object_mib": len(data) // MIB,
        "chunk_kib": chunk // 1024,
        "chunks": len(chunks),
        "pages": len(pages),
        "pages_by_depth": depths,
        "layout_choice": choose_layout(len(data)),
        "build_s": round(build_s, 2),
        "upload_pages_s": round(upload_s, 2),
        "flat_manifest_bytes": flat,
        "paged_root_bytes": len(root_page),
        "paged_leaf_page_bytes": len(leaf_pages[0]),
        "paged_path_bytes_root_plus_leaf": len(root_page) + len(leaf_pages[0]),
        "ratio_flat_over_paged_path": round(flat / (len(root_page) + len(leaf_pages[0])), 1),
        "measured_root_fetch": {"bytes": len(got_root), "s": round(root_s, 3)},
        "measured_leaf_fetch": {"bytes": len(got_leaf), "s": round(leaf_s, 3)},
        "root_hex": root,
        # Analytic scaling: the point is that this stops growing.
        "scaling": [
            {"object": label, "flat": flat_manifest_bytes(sz, chunk),
             "paged_path": len(build_index(b"\0" * (chunk * n), chunk)[1][-1]) + 22 + 256 * 32}
            for label, sz, n in [("64 MiB", 64 * MIB, 1024),
                                 ("256 MiB", 256 * MIB, 4096),
                                 ("1 GiB", 1024 * MIB, 16384)]
        ],
    }
    print(json.dumps(out, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
