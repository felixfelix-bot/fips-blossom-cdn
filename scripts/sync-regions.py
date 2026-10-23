#!/usr/bin/env python3
"""sync-regions.py — upload-only Blossom replicator for a four-region FIPS CDN.

Design corrections applied vs. the received spec (see docs/GAP-ANALYSIS.md):

  GAP-2  Blossom's /list is scoped to an uploader pubkey — there is no global
         "all blobs on this server" endpoint. So the unit of replication is a
         BLOB CONTRACT (sha256, uploader pubkey, size), not a bare hash, and
         existence is probed with HEAD /<sha256>. Without this the replica
         silently fails to copy second-generation blobs.

  GAP-5  Upload-only, never delete — correct — but a peer may have pruned a
         blob, so "present on peer, absent locally" must be an UPLOAD trigger,
         not ignored.

  GAP-6  Full-inventory diff every 60s does not scale. The peel loop here is
         an explicit seam: swap `iter_contracts` for a since-cursor later.

  GAP-7  Idempotent + timeout-protected + logged, and the caller must hold an
         flock (see the systemd unit) so runs cannot overlap.

Usage:
    sync-regions.py --self http://127.0.0.1:3000 \
                    --peer http://region-b:3000 [--peer ...] \
                    [--timeout 20] [--dry-run] [--json]

Exit codes: 0 = all peers reconciled (or dry-run), 1 = at least one peer
failed (partial progress is preserved; re-run is safe and idempotent).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import urllib.error
import urllib.request

log = logging.getLogger("sync-regions")


# ---------------------------------------------------------------- inventory


def _get(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _head_exists(url: str, timeout: float) -> bool:
    """True if the blob exists on this server (any uploader)."""
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return False
        raise


def iter_contracts(server: str, timeout: float):
    """Yield blob contract dicts from a server's /inventory.

    The /inventory endpoint (registered by our proxy) is expected to return a
    LIST OF CONTRACTS, not a bare hash list, precisely because Blossom's own
    /list is per-pubkey and cannot answer "what does this server hold".

    Contract shape: {"sha256": str, "uploader": str, "size": int}

    >>> In a production Blossom deployment without our proxy, this is where you
    >>> would call BUD-12 /list per known pubkey and union the results. The seam
    >>> is intentional — see GAP-2 and GAP-6.
    """
    body = _get(f"{server.rstrip('/')}/inventory", timeout)
    data = json.loads(body)
    items = data.get("blobs", data) if isinstance(data, dict) else data
    for it in items:
        if isinstance(it, str):
            # bare hash — tolerated, but degraded: no uploader identity.
            yield {"sha256": it, "uploader": None, "size": None}
        else:
            yield {
                "sha256": it.get("sha256") or it.get("hash"),
                "uploader": it.get("uploader") or it.get("pubkey"),
                "size": it.get("size"),
            }


def upload_blob(self_server: str, peer_server: str, sha: str, timeout: float) -> bool:
    """Pull `sha` from peer_server and PUT it to self_server.

    Upload-only: there is no delete path anywhere in this module, by design.
    """
    body = _get(f"{peer_server.rstrip('/')}/blossom/{sha}", timeout)
    got = hashlib.sha256(body).hexdigest()
    if got != sha:
        log.error("integrity failure: peer served %s for requested %s", got, sha)
        return False
    req = urllib.request.Request(
        f"{self_server.rstrip('/')}/blossom/upload",
        data=body,
        method="PUT",
        headers={"Content-Type": "application/octet-stream"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        # A duplicate upload may legitimately return 409/413-free conflict.
        if e.code == 409:
            log.info("blob %s already present on self (409) — idempotent ok", sha[:12])
            return True
        log.error("upload %s failed: HTTP %s", sha[:12], e.code)
        return False


# ------------------------------------------------------------------- driver


def reconcile(self_server: str, peer: str, timeout: float, dry_run: bool) -> dict:
    """Reconcile one peer into self. Returns a per-peer result dict."""
    result = {"peer": peer, "checked": 0, "uploaded": 0, "failed": 0, "error": None}
    try:
        contracts = list(iter_contracts(peer, timeout))
    except Exception as e:  # noqa: BLE001 — one bad peer must not abort the run
        result["error"] = f"{type(e).__name__}: {e}"
        log.warning("peer %s inventory unreachable: %s", peer, result["error"])
        return result

    for c in contracts:
        sha = c.get("sha256")
        if not sha:
            continue
        result["checked"] += 1
        try:
            # Cheap existence probe first — this is the idempotence check.
            if _head_exists(f"{self_server.rstrip('/')}/blossom/{sha}", timeout):
                continue
            # GAP-5: absent locally but present on peer => (re-)upload.
            if dry_run:
                log.info("would upload %s from %s", sha[:12], peer)
                result["uploaded"] += 1
                continue
            if upload_blob(self_server, peer, sha, timeout):
                result["uploaded"] += 1
            else:
                result["failed"] += 1
        except Exception as e:  # noqa: BLE001 — one bad blob must not kill the run
            result["failed"] += 1
            log.error("blob %s: %s: %s", sha[:12], type(e).__name__, e)
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self", dest="self_server", required=True,
                    help="this region's proxy base URL, e.g. http://127.0.0.1:3000")
    ap.add_argument("--peer", dest="peers", action="append", default=[],
                    help="a peer region's proxy base URL (repeatable)")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--json", action="store_true", help="emit a JSON summary")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if not args.peers:
        log.error("no --peer given; nothing to reconcile")
        return 1

    results = [reconcile(args.self_server, p, args.timeout, args.dry_run)
               for p in args.peers]

    summary = {
        "checked": sum(r["checked"] for r in results),
        "uploaded": sum(r["uploaded"] for r in results),
        "failed": sum(r["failed"] for r in results),
        "peers": results,
        "dry_run": args.dry_run,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        log.info("reconciled %d peer(s): checked=%d uploaded=%d failed=%d",
                 len(results), summary["checked"], summary["uploaded"],
                 summary["failed"])
        for r in results:
            if r["error"]:
                log.warning("  peer %s: %s", r["peer"], r["error"])

    # A peer being unreachable is a real failure (timeout-protected, logged),
    # but partial progress is kept and a re-run is safe.
    return 0 if all(r["error"] is None and r["failed"] == 0 for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
