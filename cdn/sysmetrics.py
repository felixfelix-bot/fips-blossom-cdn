"""Sample real bytes and CPU time, so throughput claims are measured not asserted."""
from __future__ import annotations

import os


def _read(path: str) -> list[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read().splitlines()
    except OSError:
        return []


def net_bytes() -> int:
    """Total received+transmitted bytes across non-loopback interfaces."""
    total = 0
    for line in _read("/proc/net/dev"):
        if ":" not in line:
            continue
        name, rest = line.split(":", 1)
        name = name.strip()
        if name == "lo":
            continue
        fields = rest.split()
        if len(fields) < 9:
            continue
        total += int(fields[0]) + int(fields[8])  # rx bytes, tx bytes
    return total


def cpu_seconds() -> float:
    """Process CPU time (user+sys), which excludes time blocked on I/O."""
    try:
        with open("/proc/self/stat", "r", encoding="utf-8") as fh:
            fields = fh.read().split()
        ticks = os.sysconf("SC_CLK_TCK")
        return (int(fields[13]) + int(fields[14])) / ticks
    except (OSError, IndexError, ValueError):
        return 0.0
