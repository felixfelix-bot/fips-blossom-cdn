#!/usr/bin/env python3
"""Tests for cdn-proxy.py — real HTTP, real sqlite, no mocks."""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import os
import sys
import tempfile
import threading
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "cdn_proxy", os.path.join(HERE, "..", "scripts", "cdn-proxy.py")
)
cp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cp)

PASS = FAIL = 0


def ok(n):
    global PASS; PASS += 1; print(f"  ok   - {n}")


def bad(n, d=""):
    global FAIL; FAIL += 1; print(f"  FAIL - {n}{(' :: ' + d) if d else ''}")


def verdict(cond, name, detail=""):
    """Pick ok/bad without repeating the ternary at every call site."""
    if cond:
        ok(name)
    else:
        bad(name, detail)


class FakeBlossom:
    """Minimal upstream Blossom. Its /list is deliberately per-pubkey to
    reproduce the real limitation the proxy's /inventory exists to solve."""

    def __init__(self):
        self.blobs = {}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): pass

            def do_GET(self):
                sha = self.path.rsplit("/", 1)[-1]
                d = outer.blobs.get(sha)
                if d is None:
                    self.send_error(404); return
                self.send_response(200)
                self.send_header("Content-Length", str(len(d)))
                self.end_headers(); self.wfile.write(d)

            def do_HEAD(self):
                sha = self.path.rsplit("/", 1)[-1]
                if sha in outer.blobs:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(outer.blobs[sha])))
                    self.end_headers()
                else:
                    self.send_error(404)

            def do_PUT(self):
                data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                sha = hashlib.sha256(data).hexdigest()
                outer.blobs[sha] = data
                self.send_response(200); self.end_headers()

            def do_POST(self):
                self.send_response(200); self.end_headers()

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"


def get(url, method="GET", data=None):
    req = urllib.request.Request(url, method=method, data=data)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read()


def main():
    blossom = FakeBlossom()
    dbdir = tempfile.mkdtemp()
    db = os.path.join(dbdir, "inv.db")

    cp.Handler.blossom = blossom.url
    cp.Handler.inventory = cp.Inventory(db)
    cp.Handler.sync_cmd = ["true"]
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), cp.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    try:
        # T1: upload through the proxy, proxy records the contract
        payload = b"cdn-payload-1"
        sha = hashlib.sha256(payload).hexdigest()
        st, _ = get(f"{base}/blossom/upload", "PUT", payload)
        verdict(st == 200, "PUT /blossom/upload proxies to blossom")

        st, body = get(f"{base}/inventory")
        inv = json.loads(body)["blobs"]
        if any(b["sha256"] == sha for b in inv):
            ok("/inventory lists the uploaded blob contract")
        else:
            bad("/inventory lists the uploaded blob contract", str(inv))
        if inv and inv[0].get("size") == len(payload):
            ok("contract carries size")
        else:
            bad("contract carries size", str(inv[:1]))

        # T2: /inventory survives a restart (sqlite durability, GAP-7)
        cp.Handler.inventory = cp.Inventory(db)
        st, body = get(f"{base}/inventory")
        if any(b["sha256"] == sha for b in json.loads(body)["blobs"]):
            ok("/inventory is durable across restart")
        else:
            bad("/inventory is durable across restart")

        # T3: HEAD probe through the proxy reflects blossom
        st, _ = get(f"{base}/blossom/{sha}", "HEAD")
        (ok if st == 200 else bad)("HEAD /blossom/<sha> hits blossom")
        st404 = None
        try:
            get(f"{base}/blossom/{'0'*64}", "HEAD")
        except Exception as e:
            st404 = getattr(e, "code", None)
        verdict(st404 == 404, "HEAD for a missing blob returns 404", str(st404))

        # T4: /health reports integrity
        st, body = get(f"{base}/health")
        h = json.loads(body)
        if st == 200 and h["blossom"] == "up" and h["blobs"] >= 1:
            ok("/health reports up + blob count")
        else:
            bad("/health reports up + blob count", str(h))

        # T5: /sync-now invokes the configured command
        st, body = get(f"{base}/sync-now", "POST")
        r = json.loads(body)
        if st == 200 and r["ok"] is True:
            ok("POST /sync-now runs the sync command")
        else:
            bad("POST /sync-now runs the sync command", str(r))

        # T6: a failing sync reports 500, not a false success
        cp.Handler.sync_cmd = ["false"]
        st = None
        try:
            get(f"{base}/sync-now", "POST")
        except Exception as e:
            st = getattr(e, "code", None)
        verdict(st == 500, "failing sync returns 500", str(st))
        cp.Handler.sync_cmd = ["true"]

        # T7: unknown route 404
        try:
            get(f"{base}/nope")
            bad("unknown route 404")
        except Exception as e:
            verdict(getattr(e, "code", None) == 404, "unknown route 404")
    finally:
        srv.shutdown(); blossom.srv.shutdown()

    print("---")
    print(f"pass={PASS} fail={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
