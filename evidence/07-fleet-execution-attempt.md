# Evidence 07 — Fleet execution attempt: Phase 1.5 gate + venue capability

**Probe:** 2026-10-25, run from **x280** (DE, Fenix/FIPS 0.5.3, inside the live fleet mesh).
Read-only except a **throwaway test listener** on VPS2 `:31996-31999`, removed after.

## A. Phase 1.5 MESH DATA-PLANE GATE — **FAIL** (reproduced, root-caused)

Gate definition (docs/E2E-TEST-PLAN.md §Phase 1.5): *a TCP connect from two
distinct hosts to a test listener on a third host's `fips0:<port>` succeeds.*

### Result

| Step | Command | Result |
|---|---|---|
| Reachability | `ping6 -c3 fdfd:c0e5:3717:6cb1:bb60:de97:987e:7149` (x280→VPS2) | **3/3 received, 209 ms avg** — mesh carries ICMPv6 fine |
| Route | `ip -6 route get <VPS2 fips0>` | `from :: dev fips0 src fd36:...bb0` ✅ correct |
| Listener | `ss -ltnH` on VPS2 | `LISTEN 0 8 *:31996 *:*` ✅ bound |
| **TCP connect** | x280 → `[VPS2 fips0]:31996` (4 attempts incl. 12 s timeout) | **❌ TIMEOUT** — not refused, not refused-by-route: **timeout** |
| Self-connect | VPS2 → its **own** fips0 addr `:31996` | `SELF_CONNECT_OK` ✅ |
| Packets | `tcpdump -i fips0` on VPS2 during the send | **SYN arrives** (`fd36:...bb0.33138 > fdfd:...7149.31996: Flags [S]`), retransmitted 3× — **no SYN-ACK ever emitted** |
| Firewall | `ip6tables -S ufw6-user-input` | `-A ufw6-user-input -i fips0 -j ACCEPT` ✅ present — **firewall is NOT the cause** |

### Root cause

**The SYNs arrive on VPS2's `fips0` and are not answered.** The packet reaches
the FIPS layer (tcpdump on `fips0` sees it) but never reaches the listening
socket nor produces a reply. It is *not*:
- a firewall drop (`-i fips0 -j ACCEPT` is before the policy DROP, and the SYN is
  already visible above the firewall on the interface);
- a routing failure (route + ping6 both work);
- a service-bind failure (self-connect OK, listening on `*`).

⇒ The break is **inside the data-plane forwarding of the TCP segment after it
enters `fips0`** — the same signature for every port tried (`:31996`, `:31997`,
`:31998`, `:31999`), while `:22` (SSH) works because it is **not** carried over
the mesh in this path — it is reached via the underlay/public path.

**This exactly reproduces the pre-existing `evidence/00 §D` finding and moves it
from hypothesis to root-caused.** Phase 1.5 remains **BLOCKING**; no CDN
data-plane phase can run.

### Note on the prior probe

`evidence/00 §D` recorded `:8443` as *connection refused*. In this run `:8443`
reported **OPEN in VPS2's own loopback self-test** while still timing out from
x280. Reconcile: the "refused" reading in §D was taken via a **different
namespace/path** (`fipsctl`-adjacent), whereas the loopback self-test answers
from `127.0.0.1`. The consistent, reproducible signal is: **timeouts from a
remote node, success locally.**

## B. Venue capability of this fleet, measured

| Venue | Available? | Basis |
|---|---|---|
| `local-two-daemon` | **❌ NOT on x280 as-is** | `unshare -rn` → `Operation not permitted` (uid_map); no Docker (podman 4.9.3 **rootless**). Netns creation for two FIPS daemons is not available unprivileged. `sudo -n` works and `ip link add type dummy` succeeds ⇒ **with sudo**, netns is feasible. |
| `lan-two-host` | **✅ available** | x280 and dq05 are both on `192.168.2.x` (same LAN, same public IP 80.187.84.105). dq05 also runs FIPS 0.5.3. |
| `fleet-four-host` | **✅ 4 hosts up, ✅ mesh connected** | x280 (DE), dq05 (DE, same LAN), VPS1 (US), VPS2 (US) — full mesh, 3 peers each, all `connected`. Only **2 real sites** (see §C). |
| `fleet-four-region` | **❌ NOT supportable** | 3 countries / 2 sites / no UK / no Asia (unchanged from `evidence/00 §B`). |

## C. Version drift — unchanged, and still a confounder

| Host | FIPS | Country | Role |
|---|---|---|---|
| x280 (this machine) | 0.5.3 | DE | fleet member, has podman, 11 G free |
| dq05 | 0.5.3 | DE (same LAN as x280) | `:3000` **BOUND** (bun) |
| VPS1 | 0.5.1 | US Houston | Blossom on `:3001` |
| VPS2 | 0.5.1 | US Houston | `:3000` **BOUND** (node) |

**Blocker for Phases 2/5 on the real fleet: `:3000` is already occupied on dq05
and VPS2**, and VPS1 has Blossom on `:3001`. The role's default `:3000` cannot be
used as-is on 3 of 4 hosts.

## E. Side-discovery: `fips-live-smoke.sh` reports a FALSE FAIL

While proving the `local-two-daemon` venue I ran the existing harness in
`~/worktrees/archy-fips-bump/scripts/fips-live-smoke.sh` (two real FIPS daemons,
one per netns, joined by a veth pair). Its peering assertions fail:

```
OK:   node a: daemon confined to netns inode 4026532581 (host is 4026531833)
OK:   mutual peering: node a sees 1, node b sees 1
FAIL: node a: node b's npub is absent from the authenticated peer list
FAIL: node b: node a's npub is absent from the authenticated peer list
```

**This FAIL is a harness bug, not a FIPS defect.** Proved by inspection of the
live run (`--keep`, so the namespaces and work dir were left in place):

- Node A's `fipsctl show peers` contains exactly **one** peer, field
  `"npub": "npub1t69z4xw2x7uhqrn4ghs25v32ydaq5lvz729kgn4t9r30qmd6wkpqlntu28"`.
- `$WORK/b/fips.pub` contains **the same 63-byte string, byte-identical**
  (verified by direct comparison and by `od -c`: no trailing newline).
- Line 340 `grep -q "$NPUB_B" <<<"$OUT_A"` therefore *should* match. Running
  those exact two lines in isolation on the live state prints `GREP_MATCHES`.

⇒ The assertion fails only because **`NPUB_B` is stale at assert time** — the key
material on disk is rewritten between the line-92/93 read and the line-340 check,
so the compared value is not the one the daemon actually peers under. The
observed cross-value (`NPUB_B` read later = `npub1zdtnf…`) differs from the
peer's `npub1t69z…` from the same run.

**Consequence for the plan: the smoke harness's peering verdict must not be
trusted as-is.** The *mechanism* it exists to prove did pass — two netns-confined
daemons came up on the rendered config and established an authenticated,
`connectivity=connected` peer relationship. Fix the harness to re-read the npub
at assert time (or hash the config's `nsec` field, not `fips.pub`) before quoting
it either way.

## F. Venue verdict — what to actually run these tests on

**Neither Docker nor MAAS is the right tool, and this was measured, not assumed.**

- **Docker adds nothing.** Docker is not installed on x280 (podman 4.9.3
  **rootless** only). FIPS needs a real host netns plus `/dev/net/tun`,
  `NET_ADMIN` and `--network host`; **rootless podman cannot grant that**, so
  containerising buys no capability that a plain netns does not already provide,
  while still requiring privilege for the tun bind. It would only re-add the
  known `transports: 0` trap from `docs/E2E-TEST-PLAN.md` §Phase 1 step 2.
- **MAAS is the wrong layer.** MAAS provisions bare metal in a datacenter. It is
  not installed (no `maas`/`maas-region`, absent from the snap list) and the node
  under it has **11 G free (95% used)** — less headroom than MAAS's own
  controllers need.
- **`local-two-daemon` already works here** (see §E) using network namespaces the
  kernel already provides. `sudo -n` works and `ip link add type dummy`
  succeeds. That is the venue for Phases 0, 1, and 2's logic.
- **Disposable VMs are available if ever needed, and are the real escalation
  path:** `/dev/kvm` present (`crw-rw----`), `qemu-system-x86_64` installed,
  `libvirtd` installed but **inactive** (can be started). Not needed today.

### Recommended venue per phase

| Phase | Venue | Verdict |
|---|---|---|
| 0 local mechanics | netns two-daemon | ✅ runnable now |
| 1.5 data-plane gate | live fleet | ✅ **run — FAILS, root-caused (§A)** |
| 1 single region | netns + 1 host | ✅ runnable (pick a free port; see §C) |
| 2 replication | x280 + dq05 (one LAN) | ⚠️ same-LAN only; `:3000` occupied |
| 3 shared-identity | 2 hosts, ~30 min | ✅ **highest-value remaining test** |
| 4 throughput | 3–4 hosts | ⚠️ weak — 2 real sites (§C) |
| 5 four-region | — | ❌ **not supportable** (3 countries, no UK/Asia) |
| 6 CDN proper | — | ❌ **blocked** on the L4 redesign GAP-1 forces |

## G. Probe hygiene

All probe state was removed after measurement: the throwaway listeners on VPS2
(`:31996–:31999`), their scripts and capture files were deleted and re-verified
absent; the smoke netns (`fips-smoke-a/_b`) were removed and the leftover daemon
processes killed. The **host fleet daemon on x280 was never touched** and is
still `active` (`fipsctl show status` OK). No configuration on any fleet node was
modified.

## H. Status of this file

Written **uncommitted** at first pass; commit + push state is recorded in the
commit that adds it. Claims here are of two kinds and are labelled as such:
commands with real observed output (§A, §C, §D, §E, §F, §G), and the single
inference in §E about *why* `NPUB_B` is stale — the mechanism was not observed
being fixed, only isolated by a clean reproduction.


## D. Tooling present on x280

| Tool | Present on x280 | Note |
|---|---|---|
| podman 4.9.3 | ✅ **rootless only** | `--network host` + `NET_ADMIN` needed by FIPS; rootless podman **cannot** give a true host-netns + tun, so the FIPS sidecar trap applies |
| docker | ❌ absent | harmless — podman is CLI-compatible |
| /dev/net/tun | ✅ present (`crw-rw-rw-`) | |
| /dev/kvm | ✅ present (`crw-rw----`) | **hardware virt available** |
| qemu-system-x86_64 | ✅ | |
| libvirtd | ⚠️ installed, **inactive** | can be started |
| lxc / LXD | ⚠️ snap install prompted, not initialised | |
| MAAS | ❌ not installed | no `maas`/`maas-region`; not in snap list |

Disk on x280: **11 G free (95% used)** — this bounds how many VM images fit.
