# Gap Analysis — the received spec vs. FIPS and Blossom as they actually are

Every gap is ranked by **how much it changes the build**. "Blocker" = the spec as
written cannot be implemented as described. "Correction" = implementable but the
stated mechanism is wrong and will fail if followed literally.

---

## GAP-1 — 🔴 FALSIFIED: shared-identity anycast does not work (see evidence/02)

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

## GAP-2 — 🟠 SOLVABLE WITH CONFIG (was: blocker): "compare SHA-256 inventories" needs a shared uploader key

**Spec:** *"Compare SHA-256 inventories across regions and upload missing blobs
through the Blossom upload API."*

**Reality (VERIFIED-SPEC + VERIFIED-SOURCE, 2026-10-23):** there is **no global
"all blobs on this server" endpoint.** BUD-12 `12.md:17`:

> *"The `/list/<pubkey>` endpoint MUST return a JSON array of Blob Descriptor
> objects that were uploaded by the specified pubkey"*

and `12.md:15` marks it **"optional and unrecommended"**. The reference handler
is strictly pubkey-scoped — the pubkey is the only filter axis
(`src/routes/list.ts:46,69`; DB query `JOIN owners WHERE o.pubkey = ?` at
`src/db/blobs.ts:203-208`). `list.enabled` defaults **false**
(`src/config/schema.ts:430-436`). The admin API exposes no JSON listing either
(`/admin/api/*` has only `DELETE /blobs/:sha256` and `DELETE /users/:pubkey`).

**Therefore a naive "fetch peer's /list, diff, upload" sync fails** — it sees
only that peer's own uploads, so it systematically misses blobs that *arrived by
replication* (second generation onward).

### The fix is configuration, not a new component

Three conditions make the design work as written:

1. **One shared uploader key per region** (the sync job's key) for replicated
   uploads, so every replicated blob has a known, enumerable owner.
2. `list.enabled: true` **and** `list.allowListOthers: true`.
3. Paginate with cursor: `GET /list/<K>?limit=1000&cursor=<last sha256>`
   (BUD-12 pagination `12.md:19`; reference impl caps limit at 1000,
   `src/db/blobs.ts:174`).

**⚠️ Do not rely on anonymous uploads.** With `upload.requireAuth: false` the
reference impl stores the owner as the literal string `"anonymous"`
(`src/routes/upload.ts:443`), which fails the 64-hex pubkey check
(`list.ts:27,70`) — those blobs become **permanently unenumerable over HTTP**.
If anonymous upload is enabled, the *only* true global view is out-of-band
(`SELECT sha256,size FROM blobs` against `data/sqlite.db`).

**Consequence for our proxy:** `scripts/cdn-proxy.py` already records a blob
contract per upload precisely because it cannot ask Blossom "what do you hold".
That design remains the *correct* choice for the test rig — it captures the
contract at write time instead of depending on per-pubkey listing — and the
`/inventory` endpoint is the seam where either strategy can be plugged in.

**Also verified here:** `HEAD /<sha256>` is the endorsed existence probe —
BUD-06 `06.md:12`: *"Clients that want to check whether a blob already exists
SHOULD use `HEAD /<sha256>` from BUD-01"* — and it is a pure DB lookup
(`blobs.ts:71-73`), which is what makes the reconciler's idempotence check
correct. Bonus: `GET`/`HEAD` refresh the LRU clock (`touchBlob`,
`src/routes/blobs.ts:84`), so a replica's existence probe also keeps the source
alive.

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

## GAP-5 — ⚠️ CORRECTION: "upload-only" needs a retention story — default prune is AGGRESSIVE

**Spec:** *"Do not use rsync; do not propagate deletes."*

**Reality (VERIFIED-SOURCE, well beyond what I assumed):** the reference Blossom
server **prunes by default, aggressively**. `main.ts:114-142` starts a recursive
prune loop whenever `storage.rules.length > 0 || storage.removeWhenNoOwners` —
first run after 60 s, then **every 30 s**. `prune.ts:150-190` deletes blobs whose
**last access** (or upload, if never accessed) exceeds the governing rule's
expiration. Defaults (`src/config/schema.ts:74-83`):

```
rules: [{ type: "image/*", expiration: "1 month" },
        { type: "video/*", expiration: "1 week" },
        { type: "*",       expiration: "1 week" }]
```

So **the default is a 1-week TTL on everything 🌐** — a replicated blob that is
never fetched is deleted within a week, and a region will silently lose content
its peers still advertise. This is a **correctness bug in the spec as written**,
not a tuning detail.

Three further traps found in the same read:

- **`storage.rules: []` rejects ALL uploads** — an empty rule list makes
  `getFileRule` return null, so nothing matches (`src/prune/rules.ts:100-127`).
  A well-meant "no expiry rules" config bricks the region.
- Rules are also the **MIME allowlist** — a non-matching type gets `415`.
- `storage.removeWhenNoOwners` defaults `false`; the prune loop only starts if
  there is at least one rule **or** that flag is on.

**Resolution (required config per region):**

```yaml
storage:
  rules:
    - { type: "*", expiration: "100 years" }   # or an explicit retention policy
  removeWhenNoOwners: false
delete:
  requireAuth: true        # default false in the ref impl = anyone may delete any blob
```

Plus the GAP-5 logic already in `scripts/sync-regions.py`: treat
*"present on peer, absent locally"* as a re-upload trigger, so a region that
pruned or lost a blob recovers on the next tick. (Confirmed correct against the
spec: our reconciler never issues a DELETE and the re-upload path is a valid
`PUT`.)

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

## GAP-11 — ℹ️ NOTE: BUD-04 `/mirror` exists but is *unusable* for this topology

**Not in the spec at all — but it is the obvious "why not just mirror?" question,**
so it is answered here before someone spends a day on it.

BUD-04 `PUT /mirror` (`04.md:9-20`) does exactly server-A→server-B replication:
body `{"url": "https://cdn-a.example.com/<sha>.pdf"}`, `201` new / `200`
existing; the spec's own example flow (`04.md:57-64`) *is* the replication case.
The implementation re-hashes the fetched blob and enforces the `x` tag
(`mirror.ts:518-535`), adding an owner row on dedup (`mirror.ts:551-554`).

**Why it cannot be used here (VERIFIED-SOURCE):**

1. **The SSRF guard rejects loopback and private ranges** —
   `[a === 127, "loopback (127.0.0.0/8)"]` (`src/utils/ip-guard.ts:98`), plus
   `10/8`, `172.16/12`, `192.168/16`, `::1`, ULA, link-local. Region B
   therefore **cannot** mirror from region A's `http://127.0.0.1:3000/...`, nor
   from any private mesh address.
2. **It still requires the `x` tag** ⇒ you must already know the sha256 ⇒ it
   **does not replace inventory discovery**, only the byte transfer.
3. It needs a publicly resolvable origin with a public IP.

**Resolution:** keep client-side inventory diff + `PUT /upload` as primary (as
the spec says). Revisit `/mirror` only if regions ever get public hostnames —
at which point it would simplify the transfer path but *still* need the
inventory step.

---

## GAP-12 — ℹ️ NOTE: two Blossom config traps that silently break the test

Both are config-only and both fail *quietly*, so they belong in the runbook:

- **`upload.requireAuth: false` makes blobs unenumerable.** The reference impl
  records the owner as the literal string `"anonymous"` (`upload.ts:443`),
  which fails the 64-hex pubkey check in `/list` (`list.ts:27,70`). Convenient
  for a smoke test, fatal for inventory-based replication. Prefer a shared
  uploader key (GAP-2).
- **`delete.requireAuth` should be `true`.** The impl ships it `false`, whose
  documented meaning is *"any client may delete any blob"* (`schema.ts:426`).
  For a CDN experiment that is an obvious foot-gun.

## Summary table

| # | Severity | Gap | Phase that closes it |
|---|---|---|---|
| 1 | 🔴 **falsified** | shared-identity anycast — startup-epoch restart rule | confirm in 3 |
| 2 | 🟠 config | Blossom `/list` is per-pubkey; needs shared uploader key + `list.enabled` | 2 |
| 3 | ⚠️ correction | no L4 delivery selection in the design | 3, 6 |
| 4 | ⚠️ correction | two identity planes need to be explicit | doc + 1 |
| 5 | 🟠 correctness | default 1-week LRU prune silently deletes replicated blobs | 1 |
| 6 | ⚠️ correction | full-inventory 60 s diff does not scale | 2 (seam only) |
| 7 | ⚠️ correction | "persistent" needs 3 distinct checks | 5 |
| 8 | ⚠️ correction | no FIPS version pin (fleet is on 0.5.1 and 0.5.3) | 0 |
| 9 | ℹ️ note | no delivery-quality test | 6 |
| 10 | ℹ️ note | per-path vs per-node ceiling unmeasured | 4 |
| 11 | ℹ️ note | BUD-04 `/mirror` unusable (SSRF blocks loopback) | n/a — don't try |
| 12 | ℹ️ note | two Blossom config foot-guns (`requireAuth:false`, `delete.requireAuth`) | 1 |

**Bottom line for Archipelago:** the spec is a well-formed *replication* design,
but **its central CDN premise (GAP-1) is falsified** — FIPS's startup-epoch
restart rule makes two nodes sharing an identity fight each other rather than act
as one anycast endpoint, so there is no nearest-replica selection at the network
layer and the shared sidecar actively harms mesh stability. What survives is
**four independent, mutually-replicated Blossom servers**, which is useful but is
**not a CDN**; a real delivery-selection layer would have to be an
application-level lookup keyed on the unique regional identities.

Two further correctness issues were found in the reference Blossom defaults that
would have silently destroyed replicated content (GAP-5: 1-week default prune) or
made it permanently unenumerable (GAP-12), and GAP-2 is fixable by configuration
(shared uploader key + `list.enabled`).

**Recommended sequencing given the falsification:** Phases 1–2 (single region +
two-region replication) are still worth doing — they are unaffected and produce
the replicated-store artifact. **Phase 3 becomes a ~30-minute confirmation, not
a fleet build.** Phases 4–6 should be re-scoped against whatever replaces L4.

