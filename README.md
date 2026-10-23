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

1. **The shared-identity sidecar is the load-bearing claim and it is
   unverified.** It is the only part of the design that could make this a
   *CDN* rather than *four independent Blossom servers*. FIPS's own docs never
   mention shared identity, anycast, or key cloning. It must be tested before
   anything is built on it.
2. **Blossom's `/list` is per-pubkey, not global.** A naive "compare inventories
   across regions" job cannot enumerate a peer's whole store. Replication must
   carry the blob *contract* (sha256 → uploader pubkey), not just hashes.
3. **"Upload-only, never delete" is correct and cheap** — it matches Blossom's
   content-addressed dedup semantics and avoids the one operation Blossom does
   not federate.
4. **There is no proxying media layer in the spec.** `/blossom/` + `/inventory`
   + `/sync-now` describe *replication*, not *delivery selection*. Nothing in
   the design picks a nearer replica for a viewer. That is the difference
   between "4 replicas" and "a CDN" — see `docs/ARCHITECTURE.md` §L4.
5. **FIPS has one path per node pair and no fan-out**, so per-region placement is
   the entire value proposition. Replication bandwidth is `S/(P/8)` — the
   measured LAN ceiling bounds how fast a region can be seeded.

## Status

- [x] Repo scaffold, spec captured
- [x] Gap analysis (FIPS source-read + Blossom spec-read + fleet probe)
- [x] E2E test plan
- [ ] Ansible reference role (Phases 1–2 of the plan)
- [ ] Fleet execution + evidence

## License / privacy

Private repo. Contains no keys, no npubs of live identities, no provider
credentials. Deployment files take secrets from environment / systemd
`LoadCredential` only.
