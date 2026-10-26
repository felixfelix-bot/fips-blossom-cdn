#!/usr/bin/env python3
"""A CDN node for real hosts: `origin` stores, `replica` caches, both serve.

Stdlib only, so nothing has to be installed on the fleet.

Endpoints
  GET  /health                 liveness + config
  GET  /blob/<sha256>          serve (origin: store only; replica: cache-or-fill)
  PUT  /blob                   store a body, return its sha256
  GET  /stats                  hit/miss/fill counters + bytes + cpu seconds
  POST /admin/prewarm/<key>    fill the cache without a client request
  POST /admin/crash            os._exit(9)  -- used by the failover test

Headers on a serve: X-Served-By (origin|replica), X-Cache (HIT|MISS|STORE),
X-Node (this node's name), X-Fill-Ms (upstream fill duration, replica misses).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cdn.edgecache import EdgeCache          # noqa: E402
from cdn.replica import ReplicaCache         # noqa: E402
from cdn.store import Store                  # noqa: E402
from cdn.sysmetrics import cpu_seconds, net_bytes  # noqa: E402
from cdn.upstream import LoopbackUpstream, UrlUpstream, UpstreamError  # noqa: E402

NODE_NAME = "node"
ROLE = "replica"
STORE: Store | None = None
CACHE: ReplicaCache | None = None
START = time.time()
FILLS = 0
SERVED = 0
BYTES_SERVED = 0
_LOCK = threading.Lock()


def _fill_upstream(upstream, key: str) -> bytes:
    global FILLS
    with _LOCK:
        FILLS += 1
    return upstream.fetch(key)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = None

    def log_message(self, fmt, *args):  # noqa: A002 - silence per-request noise
        pass

    def _send(self, code: int, body: bytes, headers: dict | None = None) -> None:
        global SERVED, BYTES_SERVED
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        if body:
            self.wfile.write(body)
        with _LOCK:
            SERVED += 1
            BYTES_SERVED += len(body)

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path
        if path == "/health":
            self._send(200, json.dumps({
                "role": ROLE, "node": NODE_NAME,
                "uptime_s": round(time.time() - START, 1),
            }).encode())
            return
        if path == "/stats":
            st = (CACHE.stats() if CACHE is not None else STORE.stats())
            st.update({
                "role": ROLE, "node": NODE_NAME, "fills": FILLS,
                "served": SERVED, "bytes_served": BYTES_SERVED,
                "net_bytes": net_bytes(), "cpu_seconds": round(cpu_seconds(), 3),
                "uptime_s": round(time.time() - START, 1),
            })
            self._send(200, json.dumps(st).encode())
            return
        if path.startswith("/blob/"):
            key = path.split("/", 2)[2].strip()
            if len(key) != 64:
                self._send(400, b"key must be 64 hex chars")
                return
            if ROLE == "origin":
                try:
                    self._send(200, STORE.get(key), {"X-Served-By": "origin",
                                                     "X-Cache": "STORE"})
                except KeyError:
                    self._send(404, b"not found")
                return
            assert CACHE is not None
            t0 = time.perf_counter()
            try:
                body, was_hit = CACHE.get(key)
            except UpstreamError as exc:
                self._send(502, f"upstream: {exc}".encode())
                return
            except KeyError:
                self._send(404, b"unknown")
                return
            ms = round((time.perf_counter() - t0) * 1000, 2)
            self._send(200, body, {
                "X-Served-By": "replica", "X-Cache": "HIT" if was_hit else "MISS",
                "X-Node": NODE_NAME, "X-Fill-Ms": "" if was_hit else ms,
            })
            return
        self._send(404, b"no such endpoint")

    def do_PUT(self):  # noqa: N802
        if urlparse(self.path).path != "/blob":
            self._send(404, b"no such endpoint")
            return
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n)
        key = hashlib.sha256(body).hexdigest()
        assert STORE is not None
        try:
            STORE.put(key, body)
        except ValueError as exc:
            self._send(400, str(exc).encode())
            return
        self._send(200, key.encode(), {"X-Node": NODE_NAME})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path
        if path.startswith("/admin/prewarm/"):
            key = path.split("/", 3)[3].strip()
            if CACHE is None:
                self._send(400, b"origin has no cache")
                return
            try:
                _body, was_hit = CACHE.get(key)
            except (UpstreamError, KeyError) as exc:
                self._send(502, f"prewarm failed: {exc}".encode())
                return
            self._send(200, json.dumps({"prewarmed": key, "was_hit": was_hit}).encode())
            return
        if path == "/admin/crash":
            # SIGKILL: no cleanup, no graceful socket close. A failover test that
            # kills gracefully proves less than one that does not.
            os._exit(9)
        self._send(404, b"no such endpoint")


class DualStackServer(ThreadingHTTPServer):
    """ThreadingHTTPServer listening on BOTH IPv4 and IPv6.

    fips0 is an IPv6-only point-to-point link. Binding 0.0.0.0 makes a node
    reachable on the LAN and loopback but INVISIBLE over the mesh: a peer's SYN
    reaches fips0, finds no matching socket, and the kernel answers RST. That
    looks like a firewall problem and is a bind problem. AF_INET6 with
    IPV6_V6ONLY=0 accepts both families on one socket.
    """

    address_family = socket.AF_INET6

    def server_bind(self):
        try:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        except (AttributeError, OSError):
            pass  # no IPv6 here: fall back to whatever the family allows
        super().server_bind()


def _make_server(port):
    try:
        return DualStackServer(("::", port), Handler)
    except OSError:
        # No IPv6 on this host at all - stay reachable the old way.
        return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def main() -> int:
    global NODE_NAME, ROLE, STORE, CACHE
    ap = argparse.ArgumentParser()
    ap.add_argument("role", choices=["origin", "replica"])
    ap.add_argument("--name", default=os.uname().nodename)
    ap.add_argument("--port", type=int, default=18080)
    ap.add_argument("--data-dir", required=True)
    ap.add_argument("--budget-bytes", type=int, default=256 * 1024 * 1024)
    ap.add_argument("--entry-max-bytes", type=int, default=4 * 1024 * 1024)
    ap.add_argument("--origin-url", default="")
    ap.add_argument("--loopback", action="store_true",
                    help="fill from this host's own store instead of over the net")
    ap.add_argument("--log-level", default="info",
                    help="accepted for unit-file symmetry; stdlib logging is minimal")
    args = ap.parse_args()

    ROLE = args.role
    NODE_NAME = args.name
    store_root = os.path.join(args.data_dir, "blobs")
    STORE = Store(store_root, args.budget_bytes)

    if ROLE == "replica":
        if not args.origin_url:
            print("replica needs --origin-url", file=sys.stderr)
            return 2
        upstream = LoopbackUpstream(STORE) if args.loopback else UrlUpstream(args.origin_url)
        Handler.upstream = upstream
        CACHE = ReplicaCache(upstream, STORE,
                             memory_bytes=min(args.budget_bytes, 128 * 1024 * 1024),
                             entry_max_bytes=args.entry_max_bytes)

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    srv = _make_server(args.port)
    srv.daemon_threads = True
    print(f"{ROLE} {NODE_NAME} listening on [::]:{args.port} "
          f"data={args.data_dir} budget={args.budget_bytes}", flush=True)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
