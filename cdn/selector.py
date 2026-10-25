#!/usr/bin/env python3
"""Client-side selector: resolve hash -> a verified body, via the best replica.

This is the piece that makes the whole thing a CDN rather than a bucket: the
client does not know or care which machine holds the bytes. It walks an ordered
candidate list, measures each attempt, reports the outcome back into the
catalogue, and only accepts a body whose hash matches.

Ordering guarantee: the first candidate is the fastest *measured* replica. If it
is slow, the client does not block forever — the read timeout bounds each
attempt and the next candidate is tried.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .catalogue import Catalogue, Replica, timed
from .relay import fetch_from


class NoReplicaAvailable(RuntimeError):
    """No candidate could serve a verified copy."""


@dataclass
class Result:
    body: bytes
    sha256: str
    served_by: Replica
    attempts: int
    elapsed_ms: float


class Selector:
    def __init__(
        self,
        catalogue: Catalogue,
        *,
        timeout_s: float = 5.0,
        fetcher=fetch_from,
    ) -> None:
        self.catalogue = catalogue
        self.timeout_s = timeout_s
        self._fetch = fetcher

    def get(self, sha256: str, *, max_attempts: int | None = None) -> Result:
        cands = self.catalogue.candidates()
        if not cands:
            raise NoReplicaAvailable("catalogue has no usable replica")

        errors: list[str] = []
        for attempt, sel in enumerate(cands, start=1):
            if max_attempts is not None and attempt > max_attempts:
                break
            replica = sel.replica
            try:
                body, ms = timed(
                    lambda r: self._fetch(r.base_url, sha256, self.timeout_s),
                    replica,
                )
            except Exception as exc:  # timeout, refused, 404, ...
                errors.append(f"{replica.key()}: {type(exc).__name__}")
                self.catalogue.report_failure(replica)
                continue

            actual = hashlib.sha256(body).hexdigest()
            if actual != sha256:
                # A replica served the wrong bytes. Never accept, never cache.
                errors.append(f"{replica.key()}: HASH-MISMATCH")
                self.catalogue.report_failure(replica)
                continue

            self.catalogue.report_success(replica, ms)
            return Result(
                body=body,
                sha256=sha256,
                served_by=replica,
                attempts=attempt,
                elapsed_ms=ms,
            )

        raise NoReplicaAvailable(
            "all candidates failed: " + ("; ".join(errors) or "no attempts")
        )
