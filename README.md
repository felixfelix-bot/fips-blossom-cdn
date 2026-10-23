# fips-blossom-cdn

Four-region **Blossom CDN over FIPS** — reference analysis, gap report, and an
end-to-end test plan that runs on the Hermes fleet.

## Provenance

Thomas (Archipelago) described a demo: *four servers in different regions, each
given the same `npub` as its FIPS address*, serving the same Blossom content.
He later sent a DevOps-authored brief (the "spec" below). Felix asked for:

> create a repo for this and do a gap analysis and work with the consultants to
> make a comprehensive plan to test it end to end on our hermes fleet

This repo is that deliverable.

## The spec, as received (verbatim intent, ungap-analysed)

> Build a four-region Blossom CDN (US, EU, Asia, UK) over FIPS.
> Each region has: one unique FIPS host identity `npub1REGION...`; one local
> Blossom server on `127.0.0.1:3000`; an HTTP proxy exposing `/blossom/`,
> `/inventory`, `/sync-now`; a systemd sync job every 60 s.
>
> Also run one FIPS sidecar per region. **All sidecars use the same cloned CDN
> identity (`npub1CDN_SHARED...`)** from a securely injected private-key secret;
> each sidecar peers locally with that region's unique host node. Never put
> private keys in code, logs, or Git.
>
> Replication: **upload-only.** Compare SHA-256 inventories across regions and
> upload missing blobs through the Blossom upload API. Do not use rsync; do not
> propagate deletes. Idempotent, timeout-protected, logged, persistent across
> reboots.
>
> Deliver idempotent deployment files, systemd units, firewall rules, health
> checks, rollback instructions, and smoke tests: upload in one region, verify
> the blob is replicated and retrievable in all four.
>
> *Note: the identical shared-identity sidecars are an experiment, not
> guaranteed geo-routing; the unique regional identities are the reliable
> direct endpoints.*

## Read these first

| Document | What it answers |
|---|---|
| [`docs/GAP-ANALYSIS.md`](docs/GAP-ANALYSIS.md) | Spec vs. what FIPS + Blossom actually do. Every gap, ranked. |
| [`docs/E2E-TEST-PLAN.md`](docs/E2E-TEST-PLAN.md) | Fleet-wide end-to-end experiment plan, phase by phase, with falsification criteria. |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | The layer decomposition — where the real blocker is. |
| [`docs/RUNBOOK.md`](docs/RUNBOOK.md) | Idempotent deploy, health checks, rollback. |

## Headline findings

1. **🔴 The shared-identity sidecar does not work — FALSIFIED from source.**
   FIPS draws a random *startup epoch* per process (`src/node/context.rs:30`) and
   treats a known identity presenting a different epoch as **a peer that
   restarted** (`core/src/proto/fmp/core.rs:496` → `RestartThenPromote`, then
   `close_session(...)` at `src/node/handlers/handshake.rs:2393`). Two
   shared-identity nodes therefore tear down each other's sessions in a loop —
   they are **one flapping peer**, not one anycast endpoint. Deploying the shared
   sidecar actively harms mesh stability. Full verdict:
   `evidence/02-shared-identity-verdict.md`. **This removes the CDN premise; what
   survives is four independently-replicated Blossom servers, which is useful but
   is not a CDN.**
2. **Blossom's `/list` is per-pubkey, not global.** A naive "compare inventories
   across regions" job cannot enumerate a peer's whole store. Replication must
   carry the blob *contract* (sha256 → uploader pubkey), not just hashes.
3. **"Upload-only, never delete" is correct and cheap** — it matches Blossom's
   content-addressed dedup semantics and avoids the one operation Blossom does
   not federate.
4. **There is no delivery-selection layer in the spec, and the network cannot
   provide one either** (GAP-1 falsified). `/blossom/` + `/inventory` +
   `/sync-now` describe *replication*. Nothing picks a nearer replica for a
   viewer, and — unlike the design's assumption — FIPS cannot do it implicitly.
   See `docs/ARCHITECTURE.md` §L4; a real L4 must be an application-level lookup
   over the unique regional identities.
5. **FIPS has one path per node pair and no fan-out**, so per-region placement is
   the entire value proposition. Replication bandwidth is `S/(P/8)` — the
   measured LAN ceiling bounds how fast a region can be seeded.

## Status

- [x] Repo scaffold, spec captured
- [x] Gap analysis (12 gaps; GAP-1 falsified, GAP-2/5/12 verified from source)
- [x] E2E test plan
- [x] Ansible reference role (syntax-checked; **not** executed on a host)
- [x] Evidence: fleet venue inventory, Blossom HTTP surface, shared-identity verdict
- [x] Working code + 31 tests green (`make test`)
- [ ] Phase 3 live confirmation of the shared-identity verdict
- [ ] Fleet execution (Phases 1–2, 5) — requires the mesh data-plane gap to be fixed first

## Evidence

| File | Contents |
|---|---|
| `evidence/00-venue-inventory.md` | Live fleet probe. **Max real spread: 3 countries / 2 real sites.** No UK, no Asia. |
| `evidence/01-blossom-http-surface.md` | BUD endpoint table, `/list` scoping, dedup, prune defaults, mirror SSRF block. |
| `evidence/02-shared-identity-verdict.md` | **The GAP-1 falsification**, with the startup-epoch mechanism and predicted symptoms. |

## License / privacy

Private repo. Contains no keys, no npubs of live identities, no provider
credentials. Deployment files take secrets from environment / systemd
`LoadCredential` only.
