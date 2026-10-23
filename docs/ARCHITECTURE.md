# Architecture — where the blocker actually is

## 1. The five-layer decomposition

"Build a CDN on FIPS" decomposes into five independent problems. They have very
different costs, and the received spec addresses mostly the *cheap* ones.

| # | Function | In this spec | Reality |
|---|---|---|---|
| **L1** | Origin storage (where the bytes live) | Blossom server per region, `127.0.0.1:3000` | ✅ Solved. Blossom is content-addressed (sha256), dedups, has a federated-ish HTTP API. |
| **L2** | Replication (getting bytes to the other regions) | `/inventory` + `/sync-now` + 60 s systemd job, upload-only | ⚠️ **Shape is right, mechanism has a gap** — Blossom `/list` is per-pubkey, see GAP-2. |
| **L3** | Addressing (how a viewer names the content) | Implicit: FIPS `npub` *is* the address | ✅ FIPS identity = IPv6 address is genuinely elegant and already solved. |
| **L4** | **Delivery selection (routing a viewer to the *nearest* replica)** | **The shared-identity sidecars** | ❌ **FALSIFIED from source — the mechanism cannot work.** See §3. Must become an application-layer lookup. |
| **L5** | Edge fan-out (many viewers, one origin) | Not addressed | ❌ FIPS has one path per node pair; N viewers = N unicast flows down the same path. Placement is the only lever. |

## 2. What "CDN" means here

A classic CDN's headline trick is **request coalescing** — 10 000 viewers, one
origin fetch. FIPS cannot do that: the spanning tree has *exactly one path
between any two nodes*, with no multicast, no anycast, no broadcast. Serving N
viewers from one origin is N unicast flows of the same bytes.

So the only CDN function FIPS can actually deliver is **pre-placement**: get a
copy of each blob onto a holder topologically near each viewer cluster, then let
the viewer fetch from that near holder. That is exactly what this spec is trying
to build, and it is the right instinct.

**But pre-placement only pays above a threshold.** With a measured single-path
ceiling of ~83 Mbit/s (LAN, one hop):

```
one origin at 1080p/5 Mbps  ->  16 hard / 12 prudent concurrent viewers
pre-placement starts to pay:  5 Mbps -> N=16 ; 2.5 Mbps -> N=33 ; 1 Mbps -> N=82
seed cost per region:         S/(P/8) = 96.7 s per GB
```

Below those N, four replicas are **pure overhead** — you pay 4× storage and
4×(N−1) upload bandwidth to solve a problem you do not have yet. That is not an
argument against building it; it is an argument for the test plan measuring
where the knee actually is on *our* hardware before the design assumes it.

## 3. The shared-identity sidecar — the load-bearing claim, now falsified

The spec's own note is admirably honest:

> *the identical shared-identity sidecars are an experiment, not guaranteed
> geo-routing; the unique regional identities are the reliable direct endpoints.*

Correct, and it needs to be stressed harder: **this single mechanism is the
difference between "a CDN" and "four independent Blossom servers that happen to
talk over FIPS."** Everything else in the spec is plumbing. If shared identity
does not produce nearest-replica selection, the deliverable is a **4-way
replicated blob store**, which is still useful — but it is not what "CDN" means,
and nobody should promise geo-routing on it.

### What is mechanically true (verified in FIPS source)

`src/identity/mod.rs`:

> *Node identity based on Nostr keypairs (secp256k1). The node_addr is derived
> from the public key via SHA-256, and the node's IPv6 address is the 0xfd
> prefix followed by the first 15 bytes of the node_addr.*

So clone the nsec → identical `node_addr` → identical `fd…` overlay address.
Two hosts **can be configured** to claim the same address. That much is certain.

### What actually happens: the startup-epoch restart rule (FALSIFIED)

The data plane does **not** treat two nodes with one identity as two endpoints.
It treats them as **one peer that keeps restarting**, and the mechanism is
explicit in source:

1. Every node draws a **random 8-byte startup epoch per process start**
   (`src/node/context.rs:30` — *"Random epoch generated at startup for peer
   restart detection"*; drawn at `src/node/mod.rs:999-1000`).
2. The epoch rides in the handshake as `remote_epoch`
   (`core/src/proto/fmp/core.rs:204`).
3. A known identity presenting a *different* epoch is classified as a **peer
   restart**: `core/src/proto/fmp/core.rs:496-497` maps
   `(Some(existing), Some(new)) if existing != new` →
   `InboundDecision::RestartThenPromote { peer }`.
4. The node then **destroys the existing session**
   (`src/node/handlers/handshake.rs:2393-2425`): `this_wins` is forced true by
   `remote_epoch_changed`, `close_session(&peer_node_addr)` runs, and it logs
   *"Peer restart detected during promotion, replacing stale active peer"*.

Two shared-identity nodes draw different epochs, so **each concludes the other
restarted and tears down its session — repeatedly.** There is no code path that
could pick "nearest", because every structure holds exactly **one slot per
identity**: `existing_peer_epoch` is tracked per peer identity
(`src/node/mod.rs:807`), sessions are keyed by `NodeAddr`
(`close_session(&peer_node_addr)`), and the tree keeps one active link per peer.
The cross-connection tie-breaker degenerates on equal addresses — it compares
`our_node_addr < their_node_addr` (`fmp/core.rs:45`), which is false on *both*
sides when the addresses are equal, so it is not a tie-breaker at all.

**Consequence: no anycast, and the failure is not benign** — the shared sidecar
degrades the region's mesh stability. Do not deploy it.

Full verdict + predicted log lines: `evidence/02-shared-identity-verdict.md`.
Source-level, not yet observed live — Phase 3 confirms it in ~30 minutes.

## 4. Why the design is nevertheless well-formed

Three things in the spec are *right* and worth preserving under any redesign:

1. **Unique regional identities are the reliable path.** Good instinct: never let
   the experiment be the only way to reach a region. Keep both planes.
2. **Upload-only replication.** Blossom is content-addressed; identical bytes
   dedup. Deletes do not federate anywhere in the ecosystem, so not propagating
   them is honest rather than lazy. It also makes the sync job idempotent by
   construction — the same operation repeated converges.
3. **Sidecar peering locally with the host node.** Keeps the shared-identity
   sidecar off the critical path: if it misbehaves, the host node's unique
   identity still serves that region.

## 5. Where this diverges from our prior art

Prior fleet work already established, by measurement, that:

- A FIPS spanning tree is **not** multipath; it switches paths, it does not
  combine them.
- Per-flow parallelism gains nothing on a shared path (4 streams ≈ 1 stream).
- Whether the ceiling is **per-path or per-node** is *still unmeasured* — and it
  directly decides whether k replicas give `k·P` aggregate capacity or just
  `P`. This spec implicitly assumes per-node. **Phase 4 of the test plan tests
  exactly that**, because if the ceiling is per-path, replication buys
  resilience but not throughput, and the economics change completely.

## 6. Geographic honesty

"US, EU, Asia, UK" implies four distinct regions with low RTT to their viewers.
What the fleet can actually provide is bounded by where our hosts physically are
— see `E2E-TEST-PLAN.md` §2 for the live probe result. **A four-host test with
two hosts in one country is still a valid test of the mechanism** (replication,
identity behaviour, convergence) but it is **not** a test of geo-routing, and the
report must say which it was.
