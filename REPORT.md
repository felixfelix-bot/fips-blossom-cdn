# REPORT — evidence correction and unique-identity CDN design

Status: branch `pr/evidence-correction`; corrections and conceptual architecture
complete. This is **not** a claim of CDN readiness or an accepted root cause.
The parent agent owns final review and merge. `main` at `18b0d42` was already
committed; this branch adds the correction on top and does not rewrite history.

## Delivered

- `docs/EVIDENCE-CORRECTION.md` is the authoritative interpretation. It separates
  what was observed from what was inferred, withdraws the unsupported anycast
  falsification/no-locality/root-cause/harness-safety claims, and records the
  conditionality of the epoch mechanism.
- `docs/ARCHITECTURE.md` is replaced by a conceptual **one unique npub/private
  key per machine** CDN: signed trusted endpoint discovery with expiry, client or
  local-gateway health/TTFB selection and fallback, hash-keyed cache-miss
  coalescing, verified bounded writes, availability leases/TTL storage policy,
  and the limits of hash-based norange/authorization guarantees.
- Older README/architecture/gap/test-plan/progress and all evidence files carry
  superseded banners; the two Phase-3 probe scripts carry an experimental,
  not-approved-for-rerun warning. Historical text is preserved, not silently
  rewritten.
- `README.md` now leads with the correction and links this report.

## Verification performed (offline)

Local prototype tests are run via `make test`; results are recorded in the commit
message and PR body. They exercise the HTTP proxy/reconciler against local test
servers only. No FIPS daemon, fleet host, network namespace, VM, firewall, docker
or deployment was touched. No live probe was run. No ansible playbook was run.

## Not established

- No firewall exclusion: ingress tcpdump precedes later nftables hooks and a lone
  ip6tables ACCEPT does not cover other chains/rules/policies.
- No root cause for the historical TCP timeout; the attempted explanation of the
  earlier :8443 discrepancy is unverified.
- Shared-identity anycast is neither falsified nor confirmed. Two same-identity
  nodes peering with each other are not an observer with two replicas; no winner,
  locality or epoch-loop conclusion follows. The SIGTERM restart did not verify
  the existing-peer-record/epoch precondition, and source inspected was moving
  development HEAD, not proven identical to installed FIPS 0.5.3.
- Locality is emulable on one host using controlled namespaces/netem asymmetry for
  selection mechanics, though that is not geographic or throughput proof. Rootful
  Docker can isolate TUN/netns with capabilities; MAAS is likely disproportionate,
  not impossible. `libvirtd` inactive alone does not prove installation.
- No fleet location/tooling claim in historical evidence was refreshed here.
- CORS/TLS/range, real Blossom interoperability, browser delivery, and bandwidth
  claims were not measured. Independent edge capacity and shared min-cut bound
  aggregate gains; N replicas do not imply N times throughput.

## Publication safety

Secret scan of the working tree and all local history is reported with this work.
The repository is **private** and contains historical operational IP/ASN/host
identifiers. Keep it private: a green secret scan is not a sensitivity clearance.
The already-committed `main` is published without force/rebase after review, and
the correction is published as a reviewed feature branch/PR. No live probes were
used as evidence.
