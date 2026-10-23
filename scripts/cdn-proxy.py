#!/usr/bin/env python3
"""cdn-proxy.py — the per-region HTTP surface for the Blossom CDN.

Exposes exactly three things (per the received spec):

  /blossom/...   reverse proxy to the local Blossom server (loopback only)
  /inventory     the blob CONTRACT list this region holds (see GAP-2)
  /sync-now      trigger one reconciliation immediately (POST)
  /health        readiness of fips + blossom + last sync (not in the spec, but
                 an unmonitored CDN is an outage waiting to be discovered by a
                 user — see GAP-4 on which identity to check)

Why /inventory is a contract list and not a bare hash list: Blossom's own /list
is scoped to an uploader pubkey, so there is no "give me everything on this
server" call. Our proxy tracks every blob it has ever accepted or replicated,
with its uploader, which is the only way a peer can diff correctly.
"""

from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("cdn-proxy")

DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs (
    sha256    TEXT PRIMARY KEY,
    uploader  TEXT,
    size      INTEGER,
    first_seen REAL NOT NULL
);
"""


class Inventory:
    """Durable blob contract store — survives restarts (GAP-7)."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute(DB_SCHEMA)
        self._db.commit()

    def record(self, sha256: str, uploader: str | None, size: int | None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR IGNORE INTO blobs (sha256, uploader, size, first_seen)"
                " VALUES (?,?,?,?)",
                (sha256, uploader, size, time.time()),
            )
            self._db.commit()

    def all(self) -> list[dict]:
        with self._lock:
            cur = self._db.execute(
                "SELECT sha256, uploader, size FROM blobs ORDER BY first_seen"
            )
            return [
                {"sha256": s, "uploader": u, "size": n} for s, u, n in cur.fetchall()
            ]

    def count(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM blobs").fetchone()[0]


class Handler(http.server.BaseHTTPRequestHandler):
    blossom = "http://127.0.0.1:3000"
    inventory: Inventory
    state = {"last_sync": None, "last_sync_ok": None}
    sync_cmd: list[str] = []

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    # -- helpers -------------------------------------------------------
    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _proxy(self, method: str) -> None:
        url = self.blossom + self.path
        length = int(self.headers.get("Content-Length", 0) or 0)
        data = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=data, method=method)
        for h in ("Content-Type", "Authorization"):
            if self.headers.get(h):
                req.add_header(h, self.headers[h])
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read()
                self.send_response(r.status)
                for k, v in r.headers.items():
                    if k.lower() in ("content-length", "transfer-encoding", "connection"):
                        continue
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if method != "HEAD":
                    self.wfile.write(body)
                # Record uploads so /inventory can answer across uploaders.
                # IMPORTANT: hash the REQUEST bytes, not the response body —
                # the upload response is empty, so hashing it would record the
                # sha256 of the empty string for every upload.
                if method == "PUT" and r.status < 300 and data:
                    self.inventory.record(
                        hashlib.sha256(data).hexdigest(), None, len(data)
                    )
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Length", "0")
            self.end_headers()
        except Exception as e:  # noqa: BLE001
            log.error("proxy error: %s: %s", type(e).__name__, e)
            self._json(502, {"error": f"blossom unreachable: {e}"})

    # -- routes --------------------------------------------------------
    def do_GET(self):
        if self.path == "/inventory":
            self._json(200, {"blobs": self.inventory.all()})
        elif self.path == "/health":
            self._json(200, {
                "region": os.environ.get("REGION", "unknown"),
                "blossom": "up",
                "blobs": self.inventory.count(),
                "last_sync": self.state["last_sync"],
                "last_sync_ok": self.state["last_sync_ok"],
            })
        elif self.path.startswith("/blossom/"):
            self._proxy("GET")
        else:
            self._json(404, {"error": "not found"})

    def do_HEAD(self):
        if self.path.startswith("/blossom/"):
            self._proxy("HEAD")
        else:
            self.send_response(404)
            self.end_headers()

    def do_PUT(self):
        if self.path.startswith("/blossom/upload") or self.path.startswith("/blossom/"):
            self._proxy("PUT")
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path == "/sync-now":
            import subprocess
            started = time.time()
            try:
                p = subprocess.run(self.sync_cmd, capture_output=True, text=True,
                                   timeout=300)
                ok = p.returncode == 0
                self.state["last_sync"] = started
                self.state["last_sync_ok"] = ok
                self._json(200 if ok else 500, {
                    "ok": ok,
                    "duration_s": round(time.time() - started, 2),
                    "stdout": p.stdout[-4000:],
                    "stderr": p.stderr[-2000:],
                })
            except subprocess.TimeoutExpired:
                self.state["last_sync_ok"] = False
                self._json(504, {"ok": False, "error": "sync exceeded 300s"})
        else:
            self._json(404, {"error": "not found"})


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", default="127.0.0.1:3200")
    ap.add_argument("--blossom", default="http://127.0.0.1:3000")
    ap.add_argument("--db", default="/var/lib/fips-blossom-cdn/inventory.db")
    ap.add_argument("--sync-cmd", nargs=argparse.REMAINDER, default=[])
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    os.makedirs(os.path.dirname(a.db), exist_ok=True)
    Handler.blossom = a.blossom.rstrip("/")
    Handler.inventory = Inventory(a.db)
    Handler.sync_cmd = a.sync_cmd or ["true"]

    host, _, port = a.listen.rpartition(":")
    srv = http.server.ThreadingHTTPServer((host or "127.0.0.1", int(port)), Handler)
    log.info("cdn-proxy listening on %s (blossom=%s, db=%s)", a.listen, Handler.blossom, a.db)
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
