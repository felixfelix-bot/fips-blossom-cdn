> **HISTORICAL / SUPERSEDED INTERPRETATION:** [Authoritative correction](../docs/EVIDENCE-CORRECTION.md) takes precedence.
> Observations below are preserved as originally reported, not newly verified.
> Anycast falsification, no-locality, root-cause and harness-safety claims are withdrawn
> where identified there. Do not execute the historical probe/deployment instructions.

# Evidence 00 — Fleet venue inventory

> **REDACTED FOR PUBLIC RELEASE.** This repository is public. Every public IP
> address, ASN, internal hostname and LAN subnet in the evidence files has been
> replaced with documentation-placeholder values (`198.51.100.0/24`, `AS64500+`,
> `node-*`/`vps-*`). The replacements are **not** the real topology; they are
> typed placeholders that preserve the shape of the finding without publishing
> anyone's live infrastructure. One address in the original was a **third
> party's** peer endpoint and was never ours to publish. If you are a future
> contributor: do not "restore" these values, and do not add real host addresses,
> ASNs or SSH targets to this repo.

**Probe:** 2026-10-23T14:43Z, read-only (`fipsctl show`, `ss`, `ufw status`, `curl`,
TCP connect checks). No destructive changes. Temp probe scripts deleted.

## Hosts

| Host | Reach | Arch | FIPS | svc | peers/leaf/mtu | Public IP · country · ASN | docker | :3000 | free / | RAM |
|---|---|---|---|---|---|---|---|---|---|---|
| **node-a** (local) | local | x86_64 | 0.5.3 | active | 3 / false / 1203 | 198.51.100.10 DE AS64500 Telekom | none (podman 4.9.3 rootless) | free | 11G (95%) | 5.2G |
| **node-b** | ssh via fips6 | x86_64 | 0.5.3 | active | 3 / false / 1203 | 198.51.100.10 DE AS64500 Telekom | 28.2.2 | **BOUND** bun | 12G (98%) | 6.6G |
| **vps-a** (hermes) | ssh pub+mesh | x86_64 | **0.5.1** | active | 3 / false / 1203 | 198.51.100.21 US Houston AS64501 | 29.8.1 | free, blossom on **3001**→3000 | 19G (85%) | 12.5G |
| **vps-b** (vps-b) | ssh pub+mesh | x86_64 | **0.5.1** | active | 3 / false / 1203 | 198.51.100.22 US Houston AS64501 | 29.4.3 | **BOUND** node (local curl 404) | 15G (86%) | 5.4G |
| node-d | UNREACHABLE | — | — | — | fips peer connected | LAN 192.0.2.165 | — | — | — | — |
| andre | UNREACHABLE (no sshd) | — | — | — | fips peer, udp 198.51.100.30:2121 | 198.51.100.30 **CH** AS64502 green.ch | — | — | — | — |
| t470 / netbird node-b | UNREACHABLE | — | — | — | — | — | — | — | — | — |

## A. Hosts that can run FIPS + Blossom today

- **FIPS node: 4/4 already running and connected** (node-a, node-b, vps-a, vps-b) —
  root/systemd, `/dev/net/tun` present, `sudo -n` works.
- **Blossom:** only **vps-a** has one deployed (bound `127.0.0.1:3001`).
  vps-b and node-b have docker but `:3000` is already taken; node-a has no docker
  (rootless podman present).
- **Every host is disk-tight (11–19 G free)**, and node-b is at 98%.

## B. Geographic spread — the honest limit

- **DE ×2** — node-a and node-b report the *same* public IP `198.51.100.10`
  (AS64500 Telekom). node-b sits on the same home LAN (192.0.2.0/24).
  **Not two independent EU sites.**
- **US ×2** — vps-a and vps-b are adjacent IPs (`.219`/`.51`), same city
  (Houston), same ASN.
- **CH ×1** — andre, a connected mesh peer with no reachable sshd.
- **No UK. No Asia. No 4th country.**

> **Maximum supportable claim: 3 countries / 3 ASNs / effectively 2 real sites
> + 1 third-party peer.** A "four-region US/EU/Asia/UK" CDN is **not**
> supportable on this fleet, and the final report must say so. This is exactly
> the scope statement `docs/E2E-TEST-PLAN.md` §2 requires.

## C. Live mesh peering

Stable full mesh, **4 nodes, 3 peers each**, all `connectivity=connected`:
node-a ↔ node-b, node-d, andre · node-b ↔ node-a, node-d, andre · vps-a ↔ node-d, vps-b,
andre · vps-b ↔ node-d, vps-a, andre.

- **Transport: UDP/2121 only.** `transport_peer_counts {"tcp":0,"udp":3}`;
  every peer `transport_type=udp`. No TCP-transport peer is active even though
  `fips.yaml` has `tcp bind_addr 0.0.0.0:8443` configured on node-a and vps-b.
- Mesh MTU 1203 everywhere; `estimated_mesh_size` 840–851; `is_root=false`.

## D. ⚠️ Data-plane caveat — measured, and it blocks Phase 5 as written

Mesh TCP **control/HTTP** works on vps-a (node-a→`[fdb8:…]:80` and `:443` open,
`:22` open). But **application ports on vps-b are not reachable over the mesh**:
from both node-a and node-b, `:3000`, `:9001`, `:7777` **time out**, `:8443`
returns *connection refused*, while `:22` is open and vps-b's *own* self-test to
its own fips addr `:3000` succeeds.

**Consequence:** an end-to-end CDN data-plane test over the mesh is **not
demonstrated today** — only the SSH/control path is proven across all four
nodes. Per `fips-infrastructure` reference, `Connection refused` on a peer port
means *nothing is listening*, not that routing failed; the timeouts on
`:3000/:9001/:7777` are the more interesting signal and must be diagnosed
(nftables default-drop on `fips0`, or a service bound to a non-`fips0` address)
**before** Phase 5 is attempted. Add this as a Phase-1.5 gate.

## E. Version drift

vps-a and vps-b run **FIPS 0.5.1**; node-a and node-b run **0.5.3**. Wire-compatible
within the v0.4.x/v0.5.x line, but the test must record per-node versions, and
a mixed-version mesh is a confounder for any identity-behaviour result.
