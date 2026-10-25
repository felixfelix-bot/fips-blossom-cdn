> **HISTORICAL / SUPERSEDED INTERPRETATION:** [Authoritative correction](../docs/EVIDENCE-CORRECTION.md) takes precedence.
> Observations below are preserved as originally reported, not newly verified.
> Anycast falsification, no-locality, root-cause and harness-safety claims are withdrawn
> where identified there. Do not execute the historical probe/deployment instructions.

# E2E Test Plan — four-region Blossom CDN over FIPS, on the Hermes fleet

**Urgency:** SOON — dispatch on a cheap window, complete today.
**Quota basis at planning:** z.ai gate ALLOW (ours fresh), `ollama_cloud_2` fresh
(500M), proxy alive on `127.0.0.1:9099`.

This plan is written so that **each phase produces a verdict**, and so that the
expensive phase (fleet-wide, 4 nodes) is only reached after the cheap phases have
either cleared the critical unknown or killed the design.

---

## 0. The one question this plan exists to answer — **already answered**

> Does running N FIPS sidecars under **one shared identity** produce
> nearest-replica selection (anycast), or does it corrupt routing?

**Answered: NO, and it corrupts routing.** Source read
(`evidence/02-shared-identity-verdict.md`) shows FIPS draws a random *startup
epoch* per process and classifies a known identity with a different epoch as **a
peer that restarted** (`core/src/proto/fmp/core.rs:496` → `RestartThenPromote`;
`src/node/handlers/handshake.rs:2393` → `close_session(...)`). Two
shared-identity nodes tear each other's sessions down in a loop.

**Phase 3 is therefore downgraded from "the critical unknown" to a ~30-minute
live CONFIRMATION** of a predicted log line. It no longer gates a fleet build,
because there is no longer a fleet build to gate — see §7 for the re-scope.

Secondary questions, in order of how much they change the design:

| # | Question | Phase | If the answer is bad |
|---|---|---|---|
| Q1 | Can a replication job enumerate what a peer region holds? | 2 | Replication must ship the blob contract, not just a hash list (GAP-2). |
| Q2 | Does shared identity give anycast or corruption? | 3 | **ANSWERED: corruption.** Design is "4 replicas", not "a CDN". Confirm live. |
| Q3 | Is the throughput ceiling per-path or per-node? | 4 | If per-path, k replicas buy resilience, not bandwidth — economics change. |
| Q4 | Does upload-only convergence actually converge under a 60 s cadence? | 5 | Raise cadence / switch to push-on-write. |
| Q5 | Is a viewer routed to the near replica *and* does that beat origin? | 6 | The whole CDN premise fails; report it. |

---

## 1. Test venue model

Borrowed from the fleet's existing router test-automation conventions: every
phase declares a **venue** and every phase returns one of four outcomes —
`PASS`, `FAIL`, `NOT_VERIFIED` (venue unavailable, with reason + fix), or
`INCONCLUSIVE`. **A skipped test that reports `PASS` is the bug this vocabulary
exists to prevent.**

| Venue | Hosts | What it can prove |
|---|---|---|
| `local-two-daemon` | 1 host, 2 FIPS daemons in netns/Docker | identity mechanics, config, sync logic |
| `lan-two-host` | 2 fleet hosts on one LAN | shared-identity behaviour, replication, throughput |
| `fleet-four-host` | 4 fleet hosts | the full 4-region shape, convergence, retrieval |

**Venue actually available (probed 2026-10-23, `evidence/00-venue-inventory.md`):**
4 FIPS-capable hosts all already peering, but **only 3 countries and effectively
2 real sites** (DE ×2 on one LAN/one public IP; US ×2 adjacent IPs same ASN; CH
×1 peer with no sshd). **No UK, no Asia.** A `fleet-four-host` venue exists; a
*four-region* claim does not. Also note **version drift**: VPS1/VPS2 on FIPS
**0.5.1**, x280/dq05 on **0.5.3** — a confounder to record, not to ignore.

---

## 2. Prerequisite: honest venue inventory

**Before any phase, record what the fleet actually is.** A "US/EU/Asia/UK"
design tested on 4 hosts in 2 countries is a valid mechanism test and an
**invalid geo-routing test**; the report must state which.

Collect per candidate host (read-only):

```bash
# per host: identity, arch, FIPS presence, container runtime, tun, disk, region
uname -m; uname -r
command -v fips && fips --version
systemctl is-active fips 2>/dev/null
docker --version 2>/dev/null
ls -l /dev/net/tun 2>/dev/null
df -h / | tail -1
curl -s --max-time 6 https://ipinfo.io/json | jq -r '[.ip,.country,.org]|@tsv'
ss -ltnH 'sport = :3000'    # is a Blossom already bound there?
```

Record in `evidence/00-venue-inventory.md`. **This file is an input to the
final report's scope statement.** Do not skip it and do not "tidy" it later.

---

## 3. Phases

### Phase 0 — Local mechanics (venue: `local-two-daemon`) · ~30 min

Prove the config and identity derivation before touching the fleet.

1. Build/install FIPS pinned to **v0.5.3** (latest stable tag; `master` is
   `0.6.0-dev`, 244 commits ahead — **do not test against master**).
2. Two daemons in separate network namespaces, distinct nsecs, peer them.
   Assert `fipsctl show status` → `transports: 2`, and a session establishes.
3. Derive two addresses from two nsecs; assert distinct `fd…` addresses.
4. Derive **the same** address twice from one nsec on both daemons; assert the
   addresses are byte-identical.

**Deliverable:** `evidence/01-local-mechanics.md` + the reusable
`scripts/mk-fips-config.sh` (idempotent config generator, nsec from env only).

**Falsification:** if two daemons with the *same* nsec on *one host* both start
and peer normally, note it — that is already a data point for Phase 3.

### Phase 1 — Single-region Blossom + FIPS sidecar (1 host) · ~1 h

Deploy the spec's per-region unit, exactly as written, for one region.

1. Blossom server on `127.0.0.1:3000` (container or static).
2. FIPS sidecar container, **unique** identity, `--network host` (bridge NAT
   blocks the UDP transport bind — this is a known trap; `transports: 0`
   otherwise), `NET_ADMIN`, `/dev/net/tun`.
3. HTTP proxy exposing `/blossom/`, `/inventory`, `/sync-now`.
4. systemd unit for the 60 s sync job.
5. Health check: `curl /blossom/<sha256>` round-trip through the proxy.

**Deliverable:** the ansible role skeleton + `evidence/02-single-region.md`.

**Acceptance:** upload via the proxy → fetch back by sha256 → byte-identical.
`node_exporter`-style health endpoint returns 200 with `{fips: up, blossom: up,
last_sync: <ts>}`.

### Phase 1.5 — 🔴 MESH DATA-PLANE GATE (blocking) · ~1 h

**Measured 2026-10-23 (`evidence/00-venue-inventory.md` §D): the mesh does not
currently carry application traffic to all our hosts.** From x280 and dq05,
VPS2's `:3000`, `:9001`, `:7777` **time out** and `:8443` returns *connection
refused*, while `:22` is open and VPS2's own self-test to its own fips address
`:3000` succeeds. Only the SSH/control path is proven across all four nodes.

**RE-MEASURED 2026-10-25 with root cause (`evidence/07-fleet-execution-attempt.md`
§A) — the gate FAILS, and it is not the firewall.** A throwaway TCP listener on
VPS2's `fips0` (`:31996–:31999`) is unreachable from x280 (timeout, 4/4 attempts)
while `ping6` to the same address succeeds (3/3, 209 ms), the route is correct
(`dev fips0`), VPS2 self-connects to its own `fips0` address fine, and
`ip6tables -S ufw6-user-input` already carries `-i fips0 -j ACCEPT` *before* the
policy DROP. `tcpdump -i fips0` on VPS2 **shows the SYN arriving and being
retransmitted with no SYN-ACK ever emitted** ⇒ the break is inside TCP-segment
forwarding after `fips0` ingress, not routing, not firewall, not service bind.

**No CDN data-plane phase can be attempted until this is resolved.** Diagnose, in
order — steps 1 and 2 are now **eliminated** by the above, so start at 3:

1. ~~`fips-firewall.service`~~ — **eliminated**: the accept rule is present and the
   SYN is already on the wire above it.
2. ~~Service bound to `127.0.0.1` only~~ — **eliminated**: the listener binds `*`
   and VPS2 self-connects successfully.
3. **The live lead:** why does an inbound TCP segment on `fips0` not reach the
   socket? Check FIPS's own data-plane handling — MTU/segment handling on the
   TUN path (mesh MTU is **1203** here vs `fips0` link MTU 1280), checksum/TCP-offload
   interaction on the tun device, and whether the daemon forwards native IP flows
   only for flows it has registered. Use `tcpdump` on `fips0` on **both** sides
   (confirmed: outbound SYN at x280, inbound SYN at VPS2, no reply) plus the
   daemon's own counters (`fipsctl show native-flows`, `show routing`).

**Gate:** a TCP connect from two distinct hosts to a test listener on a third
host's `fips0:<port>` succeeds. Record the exact command and result.

### Phase 2 — Replication mechanism, two regions (2 hosts) · ~2 h

**This is where GAP-2 bites.** Answer Q1 before writing the sync job properly.

1. Stand up region A and region B exactly as Phase 1, on two hosts.
2. Upload blob `X` to A. Attempt to discover it from B:
   - (a) Ask B's `/list` for A's blobs — **expect this to fail**; document the
     exact failure (scoped to uploader pubkey).
   - (b) Then implement the working path: carry the **blob contract**
     `(sha256, uploader_pubkey, size)` and probe with `HEAD /<sha256>`.
3. Implement `sync-now` in the corrected shape: fetch B's inventory contract,
   diff against local, upload missing via Blossom upload API. **Upload-only, no
   deletes, idempotent.**
4. Run it twice; assert the second run uploads zero blobs (idempotence).
5. Run it against an unreachable peer; assert it **times out cleanly and logs**,
   and does not partially corrupt local state.

**Deliverable:** `scripts/sync-regions.py` + `evidence/03-replication.md`.

**Acceptance:** blob uploaded to A appears on B within one sync interval;
second run is a no-op; network failure is non-fatal and logged.

### Phase 3 — ✅ CONFIRMATION (was: the critical unknown): shared-identity sidecars (2 hosts) · ~30 min

**Q2 is already answered from source (falsified — see §0).** This phase exists
only to *confirm the prediction live*, and to have an artifact to hand the
partner. **Pass condition is the ABSENCE of the predicted log lines, not the
presence of a connection** — a connection will form, which is exactly what makes
this trap deceptive.

Check for, on both hosts:
`Peer restart detected during promotion, replacing stale active peer` and
`Cleared stale FSP session after peer restart during promotion`
(`journalctl -u fips -f`), plus rising `parent_switches` in
`fipsctl show tree`, sampled 60× at 5 s.

If the lines appear → **prediction confirmed, shared sidecar is dead, stop.**
If they do NOT appear → the source reading is wrong somewhere; investigate before
trusting anything else in this plan.

Setup: on host A run the region-A host node (unique identity) **plus** a sidecar
using the shared CDN nsec. On host B, the same — region-B host node plus a
sidecar on the **same shared nsec**, peering locally to B's host node.

Measure, with both sidecars running:

1. **Startup:** both sidecars start; neither crashes or refuses the key.
2. **Tree stability** — `fipsctl show tree` on both, sampled 60× at 5 s
   intervals. Watch root/parent churn. **Pass condition: stable, or churn that
   settles.** `unstable_root` / repeated `parent_switches` = corruption.
3. **Session keying** — `fipsctl show sessions` and `show peers`: does one
   shared-identity peer appear once, twice, or flap between two underlays?
4. **Reachability** — from a third node, dial the shared address. Which sidecar
   answers? Does it answer *consistently*?
5. **Locality probe (the actual anycast test)** — from a node topologically
   nearer to A, measure RTT to the shared address; repeat from a node nearer to
   B. **Anycast predicts each gets the near one; if both get the same one
   regardless of position, it is NOT anycast — it is an arbitrary winner.**
6. **Negative control:** stop A's sidecar. Does B's take over? Stop both and
   confirm the host nodes' unique identities are *unaffected*.

**Outcomes and what each means:**

| Observation | Verdict | Consequence |
|---|---|---|
| Stable tree, position-dependent answers, failover works | ✅ anycast-ish | CDN premise holds; proceed to Phase 4 with confidence |
| Stable tree, but one sidecar always wins | ⚠️ no locality | Not a CDN. It's a replicated store with a designated node. |
| Root/parent churn, flapping sessions | ❌ corruption | **Do not deploy.** Report to Archipelago with evidence. |
| Either out of scope / hosts not co-located | `INCONCLUSIVE` | Say so; do not report a pass. |

**Deliverable:** `evidence/04-shared-identity.md` — raw `fipsctl` samples +
the verdict table. This is the single most valuable artifact of the whole plan.

### Phase 4 — Throughput: per-path or per-node? (3–4 hosts) · ~3 h

**Answer Q3.** Prior fleet work established that 4 parallel streams on *one*
path aggregate nothing (82.8 vs 83.6 Mbit/s). It did **not** test 4 flows from 4
different nodes over 4 different links — which is exactly what replication
depends on.

- **M1:** 2 senders × 2 receivers, link-disjoint, aggregate throughput.
- **M2:** same shape, senders on a shared path. Control.
- Interpretation: `aggregate ≈ k·P` ⇒ per-node ceiling ⇒ **replication
  multiplies capacity**. `aggregate ≈ P` ⇒ per-path ⇒ replication buys
  resilience only.

Mind the traps: `fips-firewall.service` default-drops unknown ports on `fips0`
(insert an accept rule, then delete it **by handle** and verify the chain is
byte-identical); use `ssh <node>.fips` only when something listens on that port,
otherwise use the underlay address.

**Deliverable:** `evidence/05-throughput.md`, one JSON line per run.

### Phase 5 — Four-region convergence (4 hosts) · ~4 h

The spec's own smoke test, executed.

1. All four regions deployed by the ansible role (idempotent — run **twice**,
   second run `changed=0` except genuinely converging config).
2. Upload a blob in region US; poll all four regions until present, record the
   time to converge per region.
3. Fetch the blob from all four; assert sha256 matches everywhere.
4. Reboot one host; assert the sync job resumes and re-converges without
   intervention (**persistence across reboots** — a spec requirement).
5. Delete nothing; assert deletes do not propagate (spec: upload-only).
6. Firewall: assert the Blossom port is **not** exposed publicly; only
   reachable over `fips0` / the local proxy.

**Deliverable:** `evidence/06-four-region.md` + the ansible role merged.

### Phase 6 — Delivery quality (the CDN test proper) · ~2 h

**Answer Q5.** Everything above proves *replication*. This proves *CDN*.

- From a viewer co-located with region A, fetch a blob present in all four.
  Measure time-to-first-byte and sustained rate.
- **Compare** against fetching the same blob from a deliberately *far* region.
- Anycast value exists only if near-fetch is measurably better. If near and far
  are indistinguishable, the shared-identity mechanism is decorative.

Also worth measuring, because it bounds the whole design: time to seed one GB
into a region (`S/(P/8)`) using the Phase-4 number.

---

## 4. What is explicitly NOT in scope (and will be stated in the report)

- **No geo-routing claim.** The fleet's physical spread is what the Phase-0
  inventory says it is. If that is not 4 countries, this is not a geo test.
- **No production security posture.** Test identities are throwaway; the shared
  CDN key in particular is a *test* key, injected via `LoadCredential`/env, never
  committed.
- **No payment layer.** Not part of the spec.
- **No fan-out / streaming.** FIPS cannot fan out; see `ARCHITECTURE.md` §2.
  A VOD/streaming layer is a separate project.

## 5. Secrets handling (spec requirement: keys never in code, logs, Git)

- Shared CDN nsec is supplied at deploy time via systemd `LoadCredential=` or an
  env var sourced from a gitignored `.env`. **Never** as a task argument, never
  `debug:`d, never in a rendered config committed to the repo.
- `no_log: true` on every task that touches a key.
- A pre-commit check greps the repo for `nsec1` and fails.

```bash
# tests/test-no-secrets.sh
! grep -rInE 'nsec1[0-9a-z]{20,}' --exclude-dir=.git .
```

## 6. Reporting contract

One report at `evidence/REPORT.md`, plus one file per phase. Every claim carries
either a command + real output, or is labelled `UNVERIFIED`. The report opens
with a **scope statement**:

```
Venue: <N> hosts, <M> distinct countries (<list>)
Proven: <mechanism list>
NOT proven: geo-routing (venue limit), production security posture, payment
Headline: Q2 shared-identity verdict = <result>
```

## 7. Ordered task list (for the kanban chain)

| # | Task | Depends | Venue |
|---|---|---|---|
| T1 | Venue inventory + FIPS pin check | — | local |
| T2 | `mk-fips-config.sh` idempotent generator + tests | T1 | local |
| T3 | Single-region stack (blossom + sidecar + proxy + timer) | T2 | 1 host |
| T4 | `sync-regions.py` upload-only reconciler + tests | T3 | 2 hosts |
| T5 | **Shared-identity experiment + verdict** | T4 | 2 hosts |
| T6 | Throughput M1/M2 | T5 | 3–4 hosts |
| T7 | Four-region ansible role + idempotence run | T5 | 4 hosts |
| T8 | Delivery quality (near vs far) | T7 | 4 hosts |
| T9 | Report + handover to Archipelago | T8 | — |

T5 gates everything from T6 on: if the verdict is "corruption", T6–T8 are
cancelled and T9 becomes a findings report. **Do not build the fleet-wide ansible
role before T5 returns a verdict.**

| # | Task | Depends | Venue | Status |
|---|---|---|---|---|
| T0 | Venue inventory + FIPS pin check | — | fleet | ✅ done (`evidence/00`) |
| T0b | Blossom HTTP surface recon | — | local | ✅ done (`evidence/01`) |
| T1 | Shared-identity verdict | — | source | ✅ **falsified** (`evidence/02`) |
| T1b | **Mesh data-plane gate (Phase 1.5)** | T0 | 3 hosts | 🔴 **FAIL — root-caused** (`evidence/07` §A) |
| T2 | `mk-fips-config.sh` (10 tests) | — | local | ✅ done |
| T3 | `cdn-proxy.py` (10 tests) | — | local | ✅ done |
| T4 | `sync-regions.py` (11 tests) | — | local | ✅ done |
| T5 | Phase 3 live confirmation | T1b | 2 hosts | ⬜ ~30 min |
| T6 | Two-region replication (Phase 2) | T5 | 2 hosts | ⬜ |
| T7 | Throughput M1/M2 | T6 | 3–4 hosts | ⬜ |
| T8 | Four-host convergence + reboot | T6 | 4 hosts | ⬜ |
| T9 | Delivery quality — **needs a redesigned L4** | — | — | ⏸ blocked on design |
| T10 | Report + handover to Archipelago | T8 | — | ⬜ |

## 7. Re-scope after the GAP-1 falsification

Because there is no network-layer anycast, the original goal — *"upload in one
region, verify the blob is replicated and retrievable in all four"* — is still
achievable as a **replicated blob store**, but it is **not a CDN**. Two options
for the partner, and the choice belongs to them, not to us:

- **(a) Ship the replicated store.** Four independent Blossom servers with
  upload-only convergence and unique per-region identities. All of Phases 1–2,
  4, 5 apply unchanged; drop the shared sidecar entirely. This is a real,
  demonstrable artifact and can be finished on the current plan.
- **(b) Build a real L4.** Keep the unique identities and add an explicit
  nearest-region lookup (Nostr event advertising `region → npub` + a latency
  probe, or a geo hint). Then Phase 6 becomes meaningful. This is new scope, not
  a config change.

Do not silently pick one. Present both with the falsification as the reason.
