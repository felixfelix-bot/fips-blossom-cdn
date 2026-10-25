#!/usr/bin/env python3
"""One CDN node: `origin` stores+serves, `replica` caches+serves.

Run this INSIDE the namespace that owns the node's address. It is deliberately
small and stdlib-only so the acceptance run has no dependency surface.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdn.edgecache import EdgeCache, HashMismatch  # noqa: E402

OBJECTS: dict[str, bytes] = {}


def _hash(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    role = "role"
    cache: EdgeCache | None = None
    upstream = ""

    def log_message(self, format, *args):  # noqa: A002
        pass

    def _send(self, code: int, body: bytes, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path == "/health":
            payload = json.dumps({
                "role": self.role,
                "objects": len(OBJECTS),
                "cache_entries": len(self.cache) if self.cache else 0,
                "cache_bytes": self.cache.nbytes() if self.cache else 0,
                "stats": self.cache.stats.as_dict() if self.cache else {},
            }).encode()
            self._send(200, payload)
            return

        if self.path == "/stats":
            stats = self.cache.stats.as_dict() if self.cache else {}
            self._send(200, json.dumps(stats).encode())
            return

        if self.path.startswith("/blob/"):
            key = self.path[len("/blob/"):].split("?")[0]
            if self.role == "origin":
                body = OBJECTS.get(key)
                if body is None:
                    self._send(404, b"not found")
                    return
                self._send(200, body, {"X-Served-By": "origin"})
                return
            # replica: cache-or-fill
            if self.cache is None:
                self._send(500, b"misconfigured replica")
                return
            try:
                body = self.cache.get(key)
            except KeyError:
                self._send(404, b"not found")
                return
            except HashMismatch:
                self._send(502, b"upstream sent wrong bytes")
                return
            except Exception:  # noqa: BLE001
                self._send(502, b"upstream unreachable")
                return
            self._send(200, body, {"X-Served-By": "replica"})
            return

        self._send(404, b"not found")

    def do_PUT(self):  # noqa: N802
        """Origin-side ingest: PUT /blob stores and returns the hash."""
        if self.role != "origin":
            self._send(405, b"replicas do not accept writes")
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        key = _hash(body)
        OBJECTS[key] = body
        self._send(201, key.encode())


def fetch_upstream(url: str, key: str) -> bytes:
    with urllib.request.urlopen(f"{url.rstrip('/')}/blob/{key}", timeout=10) as r:
        return r.read()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("role", choices=["origin", "replica"])
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--upstream", default="")
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--seed-kb", type=int, default=0,
                    help="origin: preload a deterministic object")
    args = ap.parse_args()

    if args.role == "origin":
        _Handler.role = "origin"
        _Handler.cache = None
        if args.seed_kb:
            body = (b"cdn-object-" * 100)[: args.seed_kb * 1024]
            OBJECTS[_hash(body)] = body
    else:
        _Handler.role = "replica"
        _Handler.upstream = args.upstream
        cache = EdgeCache(lambda k: fetch_upstream(args.upstream, k),
                          max_bytes=64 * 1024 * 1024)
        _Handler.cache = cache

    srv = http.server.ThreadingHTTPServer((args.bind, args.port), _Handler)
    print(f"{args.role} listening on {args.bind}:{args.port}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
