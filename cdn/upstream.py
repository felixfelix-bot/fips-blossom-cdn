"""Upstream resolution, with a loopback short-circuit.

The loopback case is not an optimisation detail, it is a correctness fix for a
multi-machine test: if the origin and a replica are the SAME host, an "origin"
HTTP fill would travel out of the box and back in, which measures the WAN and not
the cache. Local reads keep the origin's cost honest.
"""
from __future__ import annotations

import urllib.request


class UpstreamError(RuntimeError):
    pass


class UrlUpstream:
    """Fetch from an HTTP origin."""

    def __init__(self, base_url: str, timeout: float = 20.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def fetch(self, key: str) -> bytes:
        url = f"{self.base_url}/blob/{key}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as r:
                return r.read()
        except Exception as exc:  # noqa: BLE001
            raise UpstreamError(f"{url}: {type(exc).__name__}: {exc}") from exc


class LoopbackUpstream:
    """Serve directly from a local content source (no network hop)."""

    def __init__(self, source):
        self.source = source

    def fetch(self, key: str) -> bytes:
        try:
            return self.source.get(key)
        except KeyError as exc:
            raise UpstreamError(f"loopback miss for {key[:12]}") from exc
