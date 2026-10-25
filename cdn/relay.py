#!/usr/bin/env python3
"""Replica holder: verify before store, serve by hash.

A replica is a cache with a bounded budget plus an HTTP face over it. Bytes are
verified against the requested hash BEFORE they are stored, so a replica can
never come to hold — or serve — content that does not match its name.
"""
from __future__ import annotations

import hashlib
import http.server
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Callable

from .edgecache import EdgeCache


@dataclass
class HolderConfig:
    upstream_url: str = ""          # empty = origin (fetches nothing)
    max_bytes: int = 64 * 1024 * 1024
    max_entries: int = 1024
    timeout_s: float = 10.0


class Holder:
    """One replica: an EdgeCache plus an upstream fetcher."""

    def __init__(self, cfg: HolderConfig) -> None:
        self.cfg = cfg
        self.cache = EdgeCache(
            fetch=self._fetch_upstream,
            max_entries=cfg.max_entries,
            max_bytes=cfg.max_bytes,
        )

    def _fetch_upstream(self, sha256: str) -> bytes:
        if not self.cfg.upstream_url:
            raise KeyError(f"{sha256}: origin holds no object under this hash")
        url = f"{self.cfg.upstream_url.rstrip('/')}/blob/{sha256}"
        try:
            with urllib.request.urlopen(url, timeout=self.cfg.timeout_s) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise KeyError(f"{sha256}: upstream 404") from exc
            raise
        except urllib.error.URLError as exc:
            raise ConnectionError(f"upstream unreachable: {exc.reason}") from exc

    def get(self, sha256: str) -> bytes:
        return self.cache.get(sha256)

    def store(self, body: bytes) -> str:
        """Origin-side put: hash the body, store it, return its key."""
        key = hashlib.sha256(body).hexdigest()
        self.cache._store(key, body)  # noqa: SLF001 - origin seeding path
        return key

    # ---- HTTP -----------------------------------------------------------
    def make_handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        holder = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):  # silence test noise  # noqa: A002
                pass

            def do_GET(self):  # noqa: N802
                if not self.path.startswith("/blob/"):
                    self._send(404, b"not found")
                    return
                key = self.path[len("/blob/"):].split("?")[0]
                try:
                    body = holder.get(key)
                except KeyError:
                    self._send(404, b"not found")
                    return
                except ConnectionError:
                    self._send(502, b"upstream unreachable")
                    return
                self._send(200, body)

            def _send(self, code: int, body: bytes) -> None:
                self.send_response(code)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return Handler

    def serve(self, port: int) -> http.server.ThreadingHTTPServer:
        srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), self.make_handler())
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        return srv


def fetch_from(replica_url: str, sha256: str, timeout: float = 10.0) -> bytes:
    """Fetch one object from one replica, with a hard timeout."""
    url = f"{replica_url.rstrip('/')}/blob/{sha256}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read()
