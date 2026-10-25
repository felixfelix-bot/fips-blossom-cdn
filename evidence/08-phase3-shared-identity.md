# Evidence 08 — Phase 3 shared-identity, netns venue: prediction NOT reproduced as written

**Probe:** 2026-10-25, run on **x280** in network + mount namespaces. **Zero
fleet impact** — nothing bound a host interface, the host `fips.service` was
untouched and remained `active` throughout, and all state was torn down.

## 0. Headline

Two FIPS 0.5.3 daemons under **one shared identity** (identical nsec ⇒ identical
mesh address) **came up and held a stable, mutually-authenticated peer session**
with `parent_switches: 0` and **none** of the predicted teardown log lines, over
a 72 s window.

This does **not** clear shared-identity anycast — see §3 for why, which is the
part that matters. It means the *stated mechanism* is narrower than
`evidence/02` implies, and the strong claim is **not** supported by this run.

## 1. What was measured

Shared identity generated fresh; both daemons used the same nsec:

```
shared npub: npub1qrjmjlg2qtrmdwa36h3hr4j8kncd0p8msdtsq70jyyn4404defzsnnjxjy
daemon A address: fd3b:9305:296:8c19:93:8fee:d8e4:48e2
daemon B address: fd3b:9305:296:8c19:93:8fee:d8e4:48e2   <- IDENTICAL (one identity)
```

Both daemons alive; `peers: 1` on **each** side, `display_name: shared-peer`;
`is_root: true`; **`parent_switches: 0`** on both.

Predicted lines searched for, in both daemon logs:

| Predicted line | Hits |
|---|---|
| `Peer restart detected during promotion` | **0** |
| `stale FSP session after peer restart` | **0** |
| `stale active peer` | **0** |

Window: 15×3 s and 18×4 s (two runs). **This is not the 60×5 s the plan asks
for** — see §4.

## 2. The mechanism, read from source (corrects the reading in `evidence/02`)

The epoch comparison is **conditional on already holding that identity as a
peer**. `core/src/proto/fmp/core.rs:481-518`, `establish_inbound`:

```rust
if snap.has_existing_peer {
    match (snap.existing_peer_epoch, wire.remote_epoch) {
        (Some(existing), Some(new)) if existing != new =>
            InboundDecision::RestartThenPromote { peer },   // <- line 499
        _ if !snap.replace => InboundDecision::Rekey { peer },
        ...
    }
} else {
    InboundDecision::Promote        // <- cold start takes THIS branch
}
```

So a **cold start** — two daemons dialling each other with no prior peer record
for that identity — takes the `Promote` branch on both sides. `RestartThenPromote`
requires a *pre-existing peer record* whose stored epoch differs from the
incoming one. That is exactly what this run did, and why it produced no teardown.

**Consequence:** the trap needs a **restart into an existing peering**, not merely
two shared-identity daemons starting up. `evidence/02`'s conclusion ("two
shared-identity nodes each tear down the other's session in a loop") is not
refuted in general — it is **narrowed**: it describes the restart case, and it
is not what happens on a simultaneous cold start.

## 3. What this run does NOT establish

**It does not make shared-identity anycast viable.** Nothing here shows a
*viewer* being routed to a *near* replica. On this evidence the two daemons
present **one address** and converge on **one session** — i.e. the shape is
closer to "one endpoint wins" than to nearest-replica selection. The Phase-3
outcome table in `docs/E2E-TEST-PLAN.md` has a row for exactly this:
*"Stable tree, but one sidecar always wins → ⚠️ no locality — Not a CDN."*

The locally-observable verdict available from this run is therefore
**"⚠️ no locality"**, not "✅ anycast-ish". The locality probe (item 5 of Phase 3,
position-dependent answer) **was not run** and cannot be run on this venue,
because both daemons share one address on one host.

## 4. Honest gaps in this run

1. **Window is 72 s, not the 300 s the plan specifies.** The teardown loop could
   be slower than 72 s. Not excluded.
2. **The restart-into-existing-peering variant was not run.** This is now the
   identified decisive experiment (§2) and it is cheap — restart one daemon while
   the other holds its peer record, then watch for `RestartThenPromote`. **This is
   the single next step.**
3. **No locality probe** (§3) — needs two hosts with a real topology difference.
4. The two daemons sit on one host, so "nearest" is undefined by construction.

## 5. Harness traps found (they silently invalidate a naive netns probe)

Both are **hardcoded host paths**, not config keys, so a network namespace alone
does not isolate them. A probe that misses either reports a wrong answer rather
than failing:

| Path | Trap | Symptom observed |
|---|---|---|
| `/run/fips/control.sock` | netns-separated `fipsctl` still reads the **HOST** daemon | `peers: 4` on both probe nodes — the fleet's own peers (andre, dq05, cobrador, t440) |
| `/etc/fips/peers.deny` | fleet ACL (`deny ALL` + 9-npub allowlist) filters the test identity | `Rejected peer by ACL ... decision=denylist match`, peers stuck at 0 |

Fix: `unshare --mount --propagation private` with per-node bind mounts over
**both** `/run/fips` and `/etc/fips`. With only the socket isolated, the run looks
like a FIPS property ("shared identity never peers") when it is fleet policy.

## 8. The decisive variant WAS run — and the prediction still did not reproduce

`scripts/phase3b-restart-probe.sh` sets up the precondition §2 identifies:

1. Both daemons up under the shared identity, **stable peering confirmed**
   (`A peers=1  B peers=1`) so A holds a peer record for that identity.
2. **B is restarted**, drawing a fresh `startup_epoch`
   (`src/node/mod.rs:999-1000` — `rand::rng().fill_bytes(&mut startup_epoch)`,
   verified genuinely random per process), while A keeps its stored record.
3. Sample 30×5 s (**150 s**) for the predicted teardown lines.

Result:

```
step 1: both up under the shared identity; waiting for a stable peering...
  A peers=1  B peers=1
step 2: restarting B (draws a FRESH startup epoch) while A keeps its record...
..............................   (150s)
── final state ──
  node a: peers=1     is_root: true   parent_switches: 0
  node b: peers=1     is_root: true   parent_switches: 0
VERDICT: prediction NOT reproduced even on the restart path.
```

**Zero hits** for `Peer restart detected during promotion`, `stale FSP session
after peer restart`, `stale active peer`. The session survived B's restart with
`parent_switches: 0`.

### What this means — stated carefully

The *mechanism* in `core.rs:494-517` clearly exists and is reachable in
principle. What this run shows is that on a **plain two-node peering under one
shared identity**, restarting one side does **not** produce the predicted
teardown loop. So the strong claim in `evidence/02` — "two shared-identity nodes
tear each other's sessions down in a loop" — is **not reproduced** in this
configuration, on the cold-start path **or** the restart path.

**This does not rescue shared-identity anycast**, and it is important not to read
it that way. Reasons, kept explicit:

- The daemons present **one address** and hold **one session** — the observable
  shape is "one endpoint wins", which per the Phase-3 outcome table is
  **⚠️ no locality = not a CDN**.
- The **locality probe was not run** and cannot be on this venue (both daemons
  share one address on one host). Nearest-replica selection remains unproven.
- Whether the epoch path fires under a *different* trigger (rekey races, link
  churn, a third node, `replace` flags) is **untested**.
- **Nothing here is a fleet measurement.** It is a two-daemon local result.

### Why the epoch path plausibly did not fire (hypothesis, not measured)

The guard is `if snap.has_existing_peer` with
`(Some(existing), Some(new))`. The teardown needs **both** epochs to be `Some`
and different. If A's stored record for that identity carried `None` on either
side — e.g. the record was rebuilt rather than rekeyed, or the inbound arrived as
a fresh dial with no epoch captured — the match falls through to the `_` arms and
a `Rekey`/`ReplaceThenPromote` happens instead of `RestartThenPromote`.

`fipsctl show peers` does **not** expose an epoch field (verified: the field list
carries `rekey_in_progress`, `our_session_index`, etc., but no epoch), so this
hypothesis **cannot be confirmed from the control socket** — it needs an
instrumented build or a debug-level log capture. **Stated as unverified.**

### Remaining work (numbered, for the next worker)

1. Capture **debug-level** logs on both nodes during the restart to see which
   `InboundDecision` arm is taken, and whether `existing_peer_epoch`/`remote_epoch`
   are `Some` — this settles §8's hypothesis directly.
2. Re-run with **three** shared-identity nodes, or with link churn (drop/restore
   the veth mid-run), to test whether the epoch path fires under a different
   trigger.
3. Run the **locality probe** on a two-host venue — the only thing that can
   distinguish "anycast" from "one endpoint wins".
4. Extend the window beyond 150 s (the plan asks 300 s) to exclude a slow loop.

## 9. Reproduce

```bash
sudo bash scripts/phase3-shared-identity-probe.sh          # 24x5s default
sudo P3_SAMPLES=18 P3_INTERVAL=4 bash scripts/phase3-shared-identity-probe.sh
```

## 7. Hygiene

Namespaces (`p3-shared-a/_b`), daemons, and work dir removed after the run. Host
`fips.service` never stopped, never reconfigured: `systemctl is-active fips` →
`active` before and after. No fleet node was contacted.
