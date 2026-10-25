#!/usr/bin/env python3
"""Replica catalogue, health probing and selection.

Content identity and replica identity are deliberately separate:

  * the **hash** names the bytes,
  * the **npub / endpoint** names the machine,
  * this module chooses *which machine* to ask.

Selection is measurement-based, not geography-based. A signed advertisement says
a replica *may* serve; only a probe says it *can*, and only a timed read says it
is *fast*. That distinction is the whole reason a signature is not enough.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Replica:
    """One candidate serving endpoint, identified by its own node identity."""
    npub: str
    base_url: str
    region: str = ""
    # Runtime state, not configuration:
    healthy: bool = True
    last_ttfb_ms: float | None = None
    consecutive_failures: int = 0
    # Cooldown after a failure so a dead replica is not retried every request.
    retry_after: float = 0.0

    def key(self) -> str:
        return f"{self.npub}@{self.base_url}"


@dataclass
class Selection:
    replica: Replica
    reason: str


class Catalogue:
    """Ordered replica set with health gating and fastest-first selection.

    Ordering rule (deliberate, and tested):
      1. never select a replica in cooldown,
      2. prefer replicas that have a measured TTFB, fastest first,
      3. unmeasured replicas come *after* measured ones (a claim beats a guess —
         measurement replaces the advertisement),
      4. ties break on registration order, deterministically.
    """

    def __init__(
        self,
        replicas: list[Replica],
        *,
        cooldown_s: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._replicas = list(replicas)
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._lock = threading.Lock()

    # ---- introspection --------------------------------------------------
    def all(self) -> list[Replica]:
        with self._lock:
            return list(self._replicas)

    def available(self) -> list[Replica]:
        now = self._clock()
        with self._lock:
            return [
                r for r in self._replicas
                if r.healthy and r.retry_after <= now
            ]

    # ---- selection ------------------------------------------------------
    def candidates(self) -> list[Selection]:
        """Full ordered preference list — the failover chain."""
        live = self.available()
        measured = sorted(
            (r for r in live if r.last_ttfb_ms is not None),
            key=lambda r: (r.last_ttfb_ms, self._order(r)),
        )
        unmeasured = sorted(
            (r for r in live if r.last_ttfb_ms is None),
            key=self._order,
        )
        out = [Selection(r, "measured-fastest") for r in measured]
        out += [Selection(r, "unmeasured") for r in unmeasured]
        return out

    def select(self) -> Selection | None:
        cands = self.candidates()
        return cands[0] if cands else None

    def _order(self, replica: Replica) -> int:
        for i, r in enumerate(self._replicas):
            if r is replica:
                return i
        return len(self._replicas)

    # ---- feedback -------------------------------------------------------
    def report_success(self, replica: Replica, ttfb_ms: float) -> None:
        with self._lock:
            replica.healthy = True
            replica.consecutive_failures = 0
            replica.retry_after = 0.0
            replica.last_ttfb_ms = ttfb_ms

    def report_failure(self, replica: Replica) -> None:
        with self._lock:
            replica.consecutive_failures += 1
            replica.healthy = False
            # Exponential-ish cooldown, capped, so a flapping replica is both
            # retried eventually and not retried on every single request.
            replica.retry_after = self._clock() + min(
                self._cooldown_s * (2 ** (replica.consecutive_failures - 1)),
                60.0,
            )


def timed(fn: Callable[[Replica], bytes], replica: Replica) -> tuple[bytes, float]:
    """Run `fn` and return (result, elapsed_ms)."""
    started = time.perf_counter()
    result = fn(replica)
    return result, (time.perf_counter() - started) * 1000.0
