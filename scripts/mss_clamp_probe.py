#!/usr/bin/env python3
"""Is the ~20 KB stall caused by TCP segment size exceeding the real path MTU?

fips0 advertises MTU 1280, so TCP negotiates MSS ~1220 and builds ~1260-byte
segments. ping6 shows 1198-byte packets pass and 1248-byte packets never
arrive. If that is the whole story, clamping MSS below the real ceiling should
let the same transfer complete.

Run from a US host fetching a blob that the origin already holds, so the server
reads from its local store and the measurement is pure transport.
"""
from __future__ import annotations

import socket
import sys
import time

addr, port, key = sys.argv[1], int(sys.argv[2]), sys.argv[3]
sizes = [int(x) for x in (sys.argv[4].split(",") if len(sys.argv) > 4 else ["0"])]
BUDGET = float(sys.argv[5]) if len(sys.argv) > 5 else 60.0


def attempt(mss: int) -> dict:
    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    s.settimeout(10)
    if mss:
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_MAXSEG, mss)
    t0 = time.perf_counter()
    body = b""
    try:
        s.connect((addr, port))
        s.settimeout(BUDGET)
        s.sendall(f"GET /blob/{key} HTTP/1.0\r\n\r\n".encode())
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            body += chunk
            if time.perf_counter() - t0 > BUDGET:
                break
    except Exception as exc:  # noqa: BLE001
        return {"mss": mss or "default", "error": type(exc).__name__,
                "bytes": len(body), "seconds": round(time.perf_counter() - t0, 2)}
    finally:
        s.close()
    el = time.perf_counter() - t0
    payload = body.split(b"\r\n\r\n", 1)[-1]
    complete = b"\r\n\r\n" in body and not body.endswith(b"\r\n\r\n")
    return {"mss": mss or "default", "bytes": len(payload),
            "seconds": round(el, 2), "complete": complete}


for sz in sizes:
    print(attempt(sz), flush=True)
