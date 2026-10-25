> **HISTORICAL / SUPERSEDED INTERPRETATION:** [Authoritative correction](../docs/EVIDENCE-CORRECTION.md) takes precedence.
> Observations below are preserved as originally reported, not newly verified.
> Anycast falsification, no-locality, root-cause and harness-safety claims are withdrawn
> where identified there. Do not execute the historical probe/deployment instructions.

# Evidence 00 — Fleet venue inventory

**Probe:** 2026-10-23T14:43Z, read-only (`fipsctl show`, `ss`, `ufw status`, `curl`,
TCP connect checks). No destructive changes. Temp probe scripts deleted.

## Hosts

| Host | Reach | Arch | FIPS | svc | peers/leaf/mtu | Public IP · country · ASN | docker | :3000 | free / | RAM |
|---|---|---|---|---|---|---|---|---|---|---|
| **x280** (local) | local | x86_64 | 0.5.3 | active | 3 / false / 1203 | 198.51.100.10 DE AS64500 Telekom | none (podman 4.9.3 rootless) | free | 11G (95%) | 5.2G |
| **dq05** | ssh via fips6 | x86_64 | 0.5.3 | active | 3 / false / 1203 | 198.51.100.10 DE AS64500 Telekom | 28.2.2 | **BOUND** bun | 12G (98%) | 6.6G |
| **VPS1** (hermes) | ssh pub+mesh | x86_64 | **0.5.1** | active | 3 / false / 1203 | 198.51.100.21 US Houston AS64501 | 29.8.1 | free, blossom on **3001**→3000 | 19G (85%) | 12.5G |
| **VPS2** (testserver2) | ssh pub+mesh | x86_64 | **0.5.1** | active | 3 / false / 1203 | 198.51.100.22 US Houston AS64501 | 29.4.3 | **BOUND** node (local curl 404) | 15G (86%) | 5.4G |
| t14 | UNREACHABLE | — | — | — | fips peer connected | LAN 192.168.2.165 | — | — | — | — |
| andre | UNREACHABLE (no sshd) | — | — | — | fips peer, udp 198.51.100.30:2121 | 198.51.100.30 **CH** AS64502 green.ch | — | — | — | — |
| t470 / netbird dq05 | UNREACHABLE | — | — | — | — | — | — | — | — | — |

## A. Hosts that can run FIPS + Blossom today

- **FIPS node: 4/4 already running and connected** (x280, dq05, VPS1, VPS2) —
  root/systemd, `/dev/net/tun` present, `sudo -n` works.
- **Blossom:** only **VPS1** has one deployed (bound `127.0.0.1:3001`).
  VPS2 and dq05 have docker but `:3000` is already taken; x280 has no docker
  (rootless podman present).
- **Every host is disk-tight (11–19 G free)**, and dq05 is at 98%.

## B. Geographic spread — the honest limit

- **DE ×2** — x280 and dq05 report the *same* public IP `198.51.100.10`
  (AS64500 Telekom). dq05 sits on the same home LAN (192.168.2.x).
  **Not two independent EU sites.**
- **US ×2** — VPS1 and VPS2 are adjacent IPs (`.219`/`.51`), same city
  (Houston), same ASN.
- **CH ×1** — andre, a connected mesh peer with no reachable sshd.
- **No UK. No Asia. No 4th country.**

> **Maximum supportable claim: 3 countries / 3 ASNs / effectively 2 real sites
> + 1 third-party peer.** A "four-region US/EU/Asia/UK" CDN is **not**
> supportable on this fleet, and the final report must say so. This is exactly
> the scope statement `docs/E2E-TEST-PLAN.md` §2 requires.

## C. Live mesh peering

Stable full mesh, **4 nodes, 3 peers each**, all `connectivity=connected`:
x280 ↔ dq05, t14, andre · dq05 ↔ x280, t14, andre · VPS1 ↔ t14, testserver2,
andre · VPS2 ↔ t14, VPS1, andre.

- **Transport: UDP/2121 only.** `transport_peer_counts {"tcp":0,"udp":3}`;
  every peer `transport_type=udp`. No TCP-transport peer is active even though
  `fips.yaml` has `tcp bind_addr 0.0.0.0:8443` configured on x280 and VPS2.
- Mesh MTU 1203 everywhere; `estimated_mesh_size` 840–851; `is_root=false`.

## D. ⚠️ Data-plane caveat — measured, and it blocks Phase 5 as written

Mesh TCP **control/HTTP** works on VPS1 (x280→`[fdb8:…]:80` and `:443` open,
`:22` open). But **application ports on VPS2 are not reachable over the mesh**:
from both x280 and dq05, `:3000`, `:9001`, `:7777` **time out**, `:8443`
returns *connection refused*, while `:22` is open and VPS2's *own* self-test to
its own fips addr `:3000` succeeds.

**Consequence:** an end-to-end CDN data-plane test over the mesh is **not
demonstrated today** — only the SSH/control path is proven across all four
nodes. Per `fips-infrastructure` reference, `Connection refused` on a peer port
means *nothing is listening*, not that routing failed; the timeouts on
`:3000/:9001/:7777` are the more interesting signal and must be diagnosed
(nftables default-drop on `fips0`, or a service bound to a non-`fips0` address)
**before** Phase 5 is attempted. Add this as a Phase-1.5 gate.

## E. Version drift

VPS1 and VPS2 run **FIPS 0.5.1**; x280 and dq05 run **0.5.3**. Wire-compatible
within the v0.4.x/v0.5.x line, but the test must record per-node versions, and
a mixed-version mesh is a confounder for any identity-behaviour result.
