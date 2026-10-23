# Gap Analysis — the received spec vs. FIPS and Blossom as they actually are

Every gap is ranked by **how much it changes the build**. "Blocker" = the spec as
written cannot be implemented as described. "Correction" = implementable but the
stated mechanism is wrong and will fail if followed literally.

---

## GAP-1 — 🔴 BLOCKER (unverified premise): shared-identity anycast

**Spec:** *"All sidecars use the same cloned CDN identity (`npub1CDN_SHARED...`)
… each sidecar peers locally with that region's unique host node."*

**Reality:** mechanically, cloning is trivially possible — `src/identity/mod.rs`
confirms `node_addr` is `SHA-256(pubkey)` and the overlay IPv6 is `0xfd` +
first 15 bytes of it, so the same nsec yields the same address on every host.
**But whether the FIPS data plane then does anything reasonable is not
established anywhere.**

- No reference to shared identity, anycast, duplication, or collision in
  `docs/design/` or `src/`.
- FIPS routes on tree coordinates with greedy forwarding and elects a root by
  lowest node address — both interact with duplicate addresses.
- Peer/session state is keyed by `NodeAddr`, and the tree keeps one active link
  per peer; two nodes sharing an identity are indistinguishable to those
  structures.

**Impact:** this is the *entire CDN premise*. If it does not work, the deliverable
degrades to "four independent, mutually-replicated Blossom servers" — useful, but
not a CDN, and it must not be sold as one.

**Resolution:** Phase 3 of the test plan. Costs two hosts and about two hours.
The spec's own footnote is right to call it an experiment; it should be promoted
from footnote to **blocking prerequisite**.

---

## GAP-2 — 🔴 BLOCKER: "compare SHA-256 inventories" cannot enumerate a peer's store

**Spec:** *"Compare SHA-256 inventories across regions and upload missing blobs
through the Blossom upload API."*

**Reality:** Blossom's list endpoint is **scoped to a pubkey** — you can list
what a *given uploader* stored, not everything a server holds. There is no global
"all blobs on this server" endpoint. A sync job that fetches a peer's `/list`
and diffs will see only that peer's own uploads, i.e. it will systematically
**fail to replicate the blobs that arrived by replication** — the second
generation onward.

**Compounding:** the same sha256 may have descriptors from multiple pubkeys.

**Resolution:** the inventory unit must be the **blob contract** —
`(sha256, uploader_pubkey, size, created)` — not a bare hash list, and the probe
should be `HEAD /<sha256>` for existence. Confirmed in Phase 2. Alternatively, if
the server in use exposes BUD-12 `/list` consult it and see exactly what it
returns before designing around it.

---

## GAP-3 — ⚠️ CORRECTION: `/blossom/`, `/inventory`, `/sync-now` describe replication, not delivery

**Spec:** *"an HTTP proxy exposing `/blossom/`, `/inventory`, and `/sync-now`"*.

**Reality:** all three endpoints are about **moving bytes between regions** and
**triggering that move**. Nothing in the design takes a viewer request and
chooses a replica. That decision is delegated entirely to "the shared-identity
sidecar happens to be the nearest FIPS node" — which is GAP-1, unverified.

**Impact:** the design has no L4 (delivery selection) of its own. It relies on
the network layer to do it implicitly. That is a legitimate *strategy*, but it
means there is nothing to test in the proxy for locality — the locality test
lives in Phase 3/6, not in the proxy code.

**Resolution:** state this explicitly in the design. If anycast turns out not to
work, a real L4 is required (e.g. resolve the viewer's nearest regional identity
via an application-level lookup) — a new component, not a config change.

---

## GAP-4 — ⚠️ CORRECTION: regional identity and shared identity are two different planes

The spec describes *both* a unique `npub1REGION...` per region **and** a shared
`npub1CDN_SHARED...` sidecar per region, without saying what each is for. Reading
it back: the unique identities are the **reliable direct endpoints**; the shared
identity is the **experimental anycast plane**. That is a sound design, but it
must be explicit, because:

- Health checks should target the **unique** identity (always works).
- Any "is the CDN up?" alert built on the shared identity will flap with the
  experiment.
- The unique identity is the fallback if the shared-identity sidecar is ever
  removed.

**Resolution:** document the two planes, and make the health checks keyed to the
unique one. (Cheap, but it is the difference between a debuggable system and a
mysterious one.)

---

## GAP-5 — ⚠️ CORRECTION: "upload-only, never propagate deletes" is right, but needs a retention story

**Spec:** *"Do not use rsync; do not propagate deletes."*

**Reality:** correct and matches Blossom semantics (content-addressed, deletes do
not federate in the ecosystem). **But** upload-only replication is unbounded:
every region accumulates every blob forever. Reference Blossom servers prune
unaccessed blobs after a configured window (e.g. `1 week` for `*`, `1 month` for
images, in the reference config) and remove blobs with no owners.

**Impact:** two failure modes —
(a) a *region* may prune a blob other regions believe exists → silent
inconsistency, healing only on next re-upload;
(b) **which blob a region has is a function of its expiry config**, so inventories
are not simply "what was uploaded".

**Resolution:** set the region expiry policy explicitly and record it. Decide
whether the sync job treats "present on peer but absent locally" as a
(re-)upload trigger — it should, otherwise a pruned region never recovers.

---

## GAP-6 — ⚠️ CORRECTION: 60 s cadence + full inventory diff does not scale

**Spec:** *"a systemd sync job every 60 seconds."*

**Reality:** a naive implementation fetches the full inventory contract from all
peers every 60 s and diffs. At a few thousand blobs that is fine; the cost grows
with the **total catalogue**, not with the amount that changed. At tens of
thousands of blobs this becomes the dominant load and the job stops fitting in
its interval.

**Resolution:** cheap to keep as-is initially, but make the seam explicit — the
sync job should be written so that a cursor/since-timestamp can be added later
without redesign. Also: **use a flock** so a slow run cannot overlap the next
60 s tick.

---

## GAP-7 — ⚠️ CORRECTION: "persistent across reboots" needs the syscall-level detail

**Spec:** *"persistent across reboots."*

**Reality:** three separate things must be true, and the spec does not
distinguish them:

1. The sync timer is `systemctl enable`d (not just started).
2. The FIPS sidecar has a **stable identity across restarts** — so the key must
   be persisted, not regenerated on boot. (This is exactly why the spec says
   "securely injected private-key secret"; good, but it must be `LoadCredential`
   or an env file, **not** baked into the image or a committed config.)
3. Blossom's data dir is on a persistent volume, not a container-local layer.

**Resolution:** all three checked explicitly in Phase 5 step 4 (reboot test).

---

## GAP-8 — ⚠️ CORRECTION: FIPS version pinning

The spec does not pin a version. Upstream `jmcorgan/fips`:
**v0.5.3** is the latest stable tag (2026-10-16); `master` is `0.6.0-dev`, **244
commits ahead** and mid-refactor. Testing against master is testing a moving
target and will produce unreproducible results.

**Resolution:** pin **v0.5.3** for the test. Record the exact commit in the
report.

---

## GAP-9 — ℹ️ NOTE: the delivery-quality question is absent from the spec

The spec's smoke test is: *"upload in one region, verify the blob is replicated
and retrievable in all four."* That validates **replication**. It does **not**
validate any CDN benefit — nothing measures whether a near replica is faster
than a far one.

**Resolution:** Phase 6. Without it, the project cannot honestly claim a CDN
was built, only that four servers share content.

---

## GAP-10 — ℹ️ NOTE: throughput ceiling is unmeasured for the k-node case

Prior fleet work measured ~83 Mbit/s on one path, and showed 4 flows on one path
do not aggregate. Whether the ceiling is **per-path or per-node** decides whether
k replicas give `k·P` capacity or just resilience. The spec assumes per-node.

**Resolution:** Phase 4 (M1/M2). This is a *measurement*, not a code change, and
it changes the economics of every additional region.

---

## Summary table

| # | Severity | Gap | Phase that closes it |
|---|---|---|---|
| 1 | 🔴 blocker | shared-identity anycast unverified | 3 |
| 2 | 🔴 blocker | Blossom `/list` is per-pubkey; inventory design breaks | 2 |
| 3 | ⚠️ correction | no L4 delivery selection in the design | 3, 6 |
| 4 | ⚠️ correction | two identity planes need to be explicit | doc + 1 |
| 5 | ⚠️ correction | upload-only needs a retention policy | 5 |
| 6 | ⚠️ correction | full-inventory 60 s diff does not scale | 2 (seam only) |
| 7 | ⚠️ correction | "persistent" needs 3 distinct checks | 5 |
| 8 | ⚠️ correction | no FIPS version pin | 0 |
| 9 | ℹ️ note | no delivery-quality test | 6 |
| 10 | ℹ️ note | per-path vs per-node ceiling unmeasured | 4 |

**Bottom line for Archipelago:** the spec is a well-formed *replication* design
with one unverified load-bearing assumption and one endpoint misconception. Both
are cheap to resolve (two hosts, ~4 h), and both must be resolved before a
four-region fleet build is worth paying for.
