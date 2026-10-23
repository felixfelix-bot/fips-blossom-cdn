# Evidence 02 — Shared-identity behaviour in FIPS (the GAP-1 verdict)

**Question:** can two nodes run the *same* secp256k1 identity at different network
positions, such that traffic to that identity is answered by whichever is
nearest (anycast)?

**Answer, from source: NO — and the mechanism that kills it is named and
deterministic.** This upgrades GAP-1 from "unverified" to **falsified**, which is
the single most important result in this repo.

Source: `jmcorgan/fips` local checkout `~/repos/fips-upstream`, `master`
(0.6.0-dev, 244 commits past v0.5.3). Read-only; no build, no runtime test.
**Caveat: this is a source-level verdict, not a live measurement.** Phase 3
should still be run to confirm the predicted symptom — but it is now a
*confirmation*, not a discovery, and the prediction is specific and testable.

## 1. The identity→address chain is deterministic (as expected)

`src/identity/mod.rs` (module docs):

> *Node identity based on Nostr keypairs (secp256k1). The node_addr is derived
> from the public key via SHA-256, and the node's IPv6 address is the 0xfd
> prefix followed by the first 15 bytes of the node_addr.*

So clone the nsec → identical `NodeAddr` → identical `fd…` address. Confirmed.
**Cloning is possible; that was never the question.**

## 2. The startup epoch makes two nodes loudly distinguishable

`src/node/context.rs:30-31`:

> *Random epoch generated at startup for peer restart detection.*

`src/node/mod.rs:999-1000` (and again at `:1187-1188`):

```rust
let mut startup_epoch = [0u8; 8];
rand::rng().fill_bytes(&mut startup_epoch);
```

`src/node/mod.rs:1741-1743`:

> *Get this node's startup epoch (random per-boot tag for restart detection).*

The epoch is a **random 8-byte tag drawn per process start**, and it is carried
in the FMP handshake as `remote_epoch` (`core/src/proto/fmp/core.rs:204-205`,
`:226`, `:256`).

## 3. Epoch mismatch is treated as **peer restart**, not as a second node

`core/src/proto/fmp/core.rs:496-497`:

```rust
match (snap.existing_peer_epoch, wire.remote_epoch) {
    // Epoch mismatch → peer restart (FRC-1201 rule 2).
    (Some(existing), Some(new)) if existing != new => {
        InboundDecision::RestartThenPromote { peer }
    }
```

and `core/src/proto/fmp/core.rs:394`:

> *peer restarted, so this is no cross-connection (FRC-1201 Section 5.5).*

`src/node/handlers/handshake.rs:2386-2400` then **destroys the existing
session**:

```rust
let remote_epoch_changed =
    matches!((existing_peer.remote_epoch(), remote_epoch), (Some(old), Some(new)) if old != new);
let this_wins = remote_epoch_changed || cross_connection_winner(...);

if this_wins {
    if remote_epoch_changed {
        if self.close_session(&peer_node_addr) {
            debug!(... "Cleared stale FSP session after peer restart during promotion");
        }
        info!(... "Peer restart detected during promotion, replacing stale active peer");
    }
```

**This is the falsification.** Two shared-identity nodes each draw a *different*
random epoch at startup. Each receives the other's handshake, sees an epoch that
differs from the one on record for that identity, concludes **"the peer
restarted"**, and tears down the session it had. Then the other does the same.
The two nodes are not two endpoints of one anycast identity — to FIPS they are
**one peer that keeps restarting**, and they will fight indefinitely.

## 4. Session bookkeeping is keyed by `NodeAddr`, so there is no room for two

This is the structural reason the fighting cannot be resolved:

- `close_session(&peer_node_addr)` — sessions are keyed by peer address, not by
  link. `hypotheses`: both nodes' sessions collide in the same slot
  (`src/node/handlers/session.rs:417`, `:1639`).
- The tree keeps **one active link per peer**; the cross-connection tie-breaker
  (`core/src/proto/fmp/core.rs:40-55`) has only two inputs — address comparison
  and link direction:

  ```rust
  let we_are_smaller = our_node_addr < their_node_addr;
  if we_are_smaller { this_is_outbound } else { !this_is_outbound }
  ```

  With **equal** addresses, `we_are_smaller` is `false` on *both* sides, so the
  rule is not a tie-breaker at all here — it degenerates, and the epoch-change
  path (which always sets `this_wins = true`) is what actually decides.
- `existing_peer_epoch` is tracked **per peer identity**
  (`src/node/mod.rs:807`: *"When each peer identity's last ACCEPTED epoch change
  tore down its…"*), i.e. one epoch slot per identity — exactly one, for two
  nodes.

## 5. Predicted observable symptom (for Phase 3 to confirm)

Running two shared-identity sidecars should produce, on **both**:

- repeated `Peer restart detected during promotion, replacing stale active peer`
- repeated `Cleared stale FSP session after peer restart during promotion`
- `parent_switches` climbing steadily in `fipsctl show tree`
- a session that flaps rather than stabilises

**Pass condition for Phase 3 is therefore the ABSENCE of those lines** — not the
presence of a connection. A connection *will* form; that is what makes this trap
nasty, and it is why the test must sample `fipsctl show tree` over time rather
than check connectivity once.

## 6. Consequence for the Archipelago design

The spec's own footnote said the shared-identity sidecars are *"an experiment,
not guaranteed geo-routing"* and that *"the unique regional identities are the
reliable direct endpoints."* **The experiment has a deterministically negative
answer.** Specifically:

- **There is no anycast.** Two nodes sharing a `NodeAddr` are, to FIPS, one
  flapping peer. No mechanism in the routing path could choose "nearest".
- **The failure is not benign.** It actively tears down sessions, so the shared
  sidecar can *degrade* the region's mesh stability rather than merely sit
  unused. It should not be deployed to a production region.
- **The design must therefore be reclassified.** What remains is **four
  independent Blossom servers, mutually replicated, each reachable at its own
  unique identity.** That is a legitimate and useful artifact — a replicated
  blob store with four independent endpoints — but it is **not a CDN**, and
  nothing selects a nearer replica for a viewer.

### What a real L4 would need (since the network layer cannot do it)

Delivery selection has to be an **application-layer** decision:

1. Each region keeps its **unique** identity (the reliable endpoint).
2. A viewer resolves "nearest region" via an explicit lookup — e.g. a Nostr
   event listing `(region → npub, geo/latency hint)`, or a latency probe against
   all four known npubs and pick the fastest.
3. The viewer then fetches from that region's own identity.

That is a new component, not a config change — and it is the honest scope of the
work if "CDN" is the actual goal.

## 7. Status and honesty

| Statement | Confidence |
|---|---|
| Same nsec ⇒ same NodeAddr ⇒ same fd… address | **VERIFIED** (`identity/mod.rs`) |
| Startup epoch is a random per-process tag | **VERIFIED** (`context.rs:30`, `mod.rs:999`) |
| Epoch mismatch ⇒ `RestartThenPromote` + session teardown | **VERIFIED** (`fmp/core.rs:496`, `handshake.rs:2386-2400`) |
| Session/tree state keyed by NodeAddr with one slot per identity | **VERIFIED** (handshake + `handshake.rs:2499-2510`) |
| ⇒ two shared-identity nodes flap and never stabilise | **INFERRED from the above — strongly, but not yet observed live** |
| Cross-connection tie-break degenerates on equal addresses | **VERIFIED** (`fmp/core.rs:45-53`) |

**Not done:** no live two-sidecar experiment. Phase 3 remains the confirmation
step, and it is now a ~30-minute check against a specific predicted log line
rather than an open investigation.

## 8. Method note (why this took 3 600 s and still timed out)

The first attempt at this question was dispatched as a subagent with a 60-minute
cap; it accumulated 102 API calls and **timed out without emitting a verdict**,
having read broadly across `src/node/**` without converging. `~/repos/fips-upstream`
is a large Rust tree and the relevant facts live in three files
(`identity/mod.rs`, `proc`-level `startup_epoch`, `fmp/core.rs` destruction
rule). **Lesson: pin the file list in the delegation context.** A second,
narrow pass over an explicit 3-file list answered it in minutes.
