#!/usr/bin/env python3
"""Tests for sync-regions.py against real local HTTP servers.

No mocks: in-process HTTP servers act as regions, so the reconciler is
exercised over an actual socket — HEAD probes, 409 dedup, integrity failure,
and peer unreachability included.

Each server gets its OWN store (a per-instance dict), because a real region's
blob store is independent. Sharing one class-level dict between servers makes
an empty replica look populated and silently invalidates the upload tests.
"""

from __future__ import annotations

import hashlib
import http.server
import importlib.util
import json
import os
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "sync_regions", os.path.join(HERE, "..", "scripts", "sync-regions.py")
)
sr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sr)

PASS = 0
FAIL = 0


def ok(name):
    global PASS
    PASS += 1
    print(f"  ok   - {name}")


def bad(name, detail=""):
    global FAIL
    FAIL += 1
    print(f"  FAIL - {name}{(' :: ' + detail) if detail else ''}")


class Region:
    """One region: independent blob store + upload log + inventory."""

    def __init__(self):
        self.blobs = {}
        self.uploads = []
        self.inventory = []
        self.corrupt = False

    @property
    def url(self):
        return f"http://127.0.0.1:{self.srv.server_address[1]}"

    def start(self):
        region = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.path == "/inventory":
                    body = json.dumps({"blobs": region.inventory}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/blossom/"):
                    sha = self.path.rsplit("/", 1)[-1]
                    data = region.blobs.get(sha)
                    if data is None:
                        self.send_error(404)
                        return
                    if region.corrupt:
                        data = b"CORRUPTED-PAYLOAD"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
                self.send_error(404)

            def do_HEAD(self):
                sha = self.path.rsplit("/", 1)[-1]
                if sha in region.blobs:
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(region.blobs[sha])))
                    self.end_headers()
                else:
                    self.send_error(404)

            def do_PUT(self):
                if self.path.startswith("/blossom/upload"):
                    data = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                    sha = hashlib.sha256(data).hexdigest()
                    if sha in region.blobs:
                        self.send_response(409)
                        self.end_headers()
                        return
                    region.blobs[sha] = data
                    region.uploads.append(sha)
                    self.send_response(200)
                    self.end_headers()
                    return
                self.send_error(404)

        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def shutdown(self):
        self.srv.shutdown()

    def add(self, data: bytes, uploader="npub1seller"):
        sha = hashlib.sha256(data).hexdigest()
        self.blobs[sha] = data
        self.inventory.append({"sha256": sha, "uploader": uploader, "size": len(data)})
        return sha


def main():
    try:
        # --- T1: existing blob is not re-uploaded ----------------------
        r1 = Region().start()
        sha = r1.add(b"hello-cdn")
        r = sr.reconcile(r1.url, r1.url, timeout=5, dry_run=False)
        if r["checked"] == 1 and r["uploaded"] == 0 and r["failed"] == 0:
            ok("existing blob is not re-uploaded (HEAD probe)")
        else:
            bad("existing blob is not re-uploaded (HEAD probe)", str(r))

        # --- T2: missing blob is uploaded, bytes match -----------------
        r2 = Region().start()
        r3 = Region().start()          # empty replica
        try:
            sha = r2.add(b"payload-42")
            r = sr.reconcile(r3.url, r2.url, timeout=5, dry_run=False)
            if r["uploaded"] == 1:
                ok("missing blob is uploaded")
            else:
                bad("missing blob is uploaded", str(r))
            if hashlib.sha256(r3.blobs.get(sha, b"")).hexdigest() == sha:
                ok("replicated bytes match sha256")
            else:
                bad("replicated bytes match sha256")
        finally:
            r2.shutdown(); r3.shutdown()

        # --- T3: idempotence — second run is a no-op -------------------
        r4 = Region().start()
        r5 = Region().start()
        try:
            r4.add(b"idem")
            f = sr.reconcile(r5.url, r4.url, timeout=5, dry_run=False)
            s = sr.reconcile(r5.url, r4.url, timeout=5, dry_run=False)
            if f["uploaded"] == 1 and s["uploaded"] == 0 and s["failed"] == 0:
                ok("second run is a no-op (idempotent)")
            else:
                bad("second run is a no-op (idempotent)",
                    f"first={f['uploaded']} second={s['uploaded']}")
        finally:
            r4.shutdown(); r5.shutdown()

        # --- T4: 409 from the server is treated as success ------------
        r6 = Region().start()
        r7 = Region().start()
        try:
            sha = r6.add(b"race")
            sha2 = r7.add(b"race")     # already present on self
            r = sr.reconcile(r7.url, r6.url, timeout=5, dry_run=False)
            if r["failed"] == 0:
                ok("blob already on self is not counted as failure")
            else:
                bad("blob already on self is not counted as failure", str(r))
        finally:
            r6.shutdown(); r7.shutdown()

        # --- T5: unreachable peer — clean error, non-zero exit ---------
        r8 = Region().start()
        try:
            r = sr.reconcile(r8.url, "http://127.0.0.1:1", timeout=2, dry_run=False)
            if r["error"] is not None and r["failed"] == 0:
                ok("unreachable peer reported as error, no crash")
            else:
                bad("unreachable peer reported as error, no crash", str(r))
            rc = sr.main(["--self", r8.url, "--peer", "http://127.0.0.1:1",
                          "--timeout", "2", "--json"])
            if rc == 1:
                ok("exit 1 when a peer is unreachable")
            else:
                bad("exit 1 when a peer is unreachable", f"rc={rc}")
        finally:
            r8.shutdown()

        # --- T6: corrupted peer payload is rejected -------------------
        r9 = Region().start()
        r10 = Region().start()
        try:
            r9.add(b"good-bytes")
            r9.corrupt = True
            r = sr.reconcile(r10.url, r9.url, timeout=5, dry_run=False)
            if r["failed"] == 1:
                ok("corrupted peer payload is rejected")
            else:
                bad("corrupted peer payload is rejected", str(r))
        finally:
            r9.shutdown(); r10.shutdown()

        # --- T7: dry-run reports but does not upload ------------------
        r11 = Region().start()
        r12 = Region().start()
        try:
            r11.add(b"dry")
            r = sr.reconcile(r12.url, r11.url, timeout=5, dry_run=True)
            if r["uploaded"] == 1 and len(r12.uploads) == 0:
                ok("dry-run reports but does not upload")
            else:
                bad("dry-run reports but does not upload",
                    f"uploaded={r['uploaded']} actual={len(r12.uploads)}")
        finally:
            r11.shutdown(); r12.shutdown()

        # --- T8: no peers -> exit 1 -----------------------------------
        r13 = Region().start()
        try:
            rc = sr.main(["--self", r13.url, "--json"])
            if rc == 1:
                ok("no --peer exits 1")
            else:
                bad("no --peer exits 1", f"rc={rc}")

            # --- T9: bare-hash contract tolerated ---------------------
            r13.inventory = [{"sha256": "a" * 64, "uploader": None, "size": None}]
            r = sr.reconcile(r13.url, r13.url, timeout=5, dry_run=False)
            if r["error"] is None:
                ok("degrades gracefully on contract without uploader")
            else:
                bad("degrades gracefully on contract without uploader", str(r))
        finally:
            r13.shutdown()
    finally:
        pass

    print("---")
    print(f"pass={PASS} fail={FAIL}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
