#!/usr/bin/env python3
"""Summarise a tcpdump text capture of one TCP service, per client connection.

Answer the question the shell could not: how many clients connected, how many
received payload, and what the stuck ones actually did.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict

SERVICE_PORT = 18080

line_re = re.compile(
    r"IP6 (?P<src>[0-9a-f:]+)\.(?P<sp>\d+) > (?P<dst>[0-9a-f:]+)\.(?P<dp>\d+): "
    r"Flags \[(?P<flags>[^\]]*)\].*?(?:length (?P<len>\d+))?$"
)

conns = defaultdict(lambda: {
    "syn": 0, "synack": 0, "fin": 0, "rst": 0,
    "srv_pkts": 0, "srv_bytes": 0, "cli_pkts": 0, "cli_bytes": 0,
})


def main(path: str) -> int:
    for raw in open(path, errors="replace"):
        m = line_re.search(raw.strip())
        if not m:
            continue
        sp, dp = int(m["sp"]), int(m["dp"])
        flags = m["flags"]
        ln = int(m["len"]) if m["len"] else 0
        if dp == SERVICE_PORT:
            cport, direction = sp, "c2s"
        elif sp == SERVICE_PORT:
            cport, direction = dp, "s2c"
        else:
            continue
        c = conns[cport]
        if direction == "c2s":
            c["cli_pkts"] += 1
            c["cli_bytes"] += ln
            if "S" in flags and "." not in flags:
                c["syn"] += 1
            if "F" in flags:
                c["fin"] += 1
            if "R" in flags:
                c["rst"] += 1
        else:
            c["srv_pkts"] += 1
            c["srv_bytes"] += ln
            if "S" in flags and "." in flags:
                c["synack"] += 1
            if "F" in flags:
                c["fin"] += 1
            if "R" in flags:
                c["rst"] += 1

    print(f"client connections seen: {len(conns)}")
    connected = [p for p, c in conns.items() if c["synack"]]
    got_data = [p for p, c in conns.items() if c["srv_bytes"] > 0]
    no_data = [p for p, c in conns.items() if c["synack"] and c["srv_bytes"] == 0]
    finned = [p for p, c in conns.items() if c["fin"]]
    rst = [p for p, c in conns.items() if c["rst"]]

    print(f"handshake completed : {len(connected)}")
    print(f"received payload    : {len(got_data)}")
    print(f"handshake but NO payload: {len(no_data)}")
    print(f"closed with FIN     : {len(finned)}")
    print(f"reset               : {len(rst)}")
    print()
    print("stuck connections (connected, never got a byte):")
    for p in sorted(no_data)[:20]:
        c = conns[p]
        print(f"  port {p}: cli_pkts={c['cli_pkts']} cli_bytes={c['cli_bytes']} "
              f"srv_pkts={c['srv_pkts']} srv_bytes={c['srv_bytes']}")
    print()
    print("bytes delivered per connection (service -> client):")
    for p, c in sorted(conns.items(), key=lambda kv: kv[1]["srv_bytes"]):
        print(f"  port {p}: srv_bytes={c['srv_bytes']:6d} "
              f"srv_pkts={c['srv_pkts']:4d} fin={bool(c['fin'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
