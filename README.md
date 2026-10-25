# fips-blossom-cdn

Experiment repository for Blossom delivery over FIPS. **Not a production CDN.**

## Authoritative current findings

Read [the evidence correction](docs/EVIDENCE-CORRECTION.md) first. It supersedes
older conclusions in the evidence, gap analysis, test plan and progress log.

- The recorded remote TCP timeout is real as a historical observation; its root
  cause is unresolved. An ingress SYN and one ACCEPT rule do not exclude nftables.
- Two same-identity nodes peering with each other do not test an observer choosing
  between replicas. Shared-identity anycast remains unproven, not falsified.
- Graceful restart did not verify retention of the existing peer epoch. Source
  inspection of moving development HEAD was not pinned to installed FIPS 0.5.3.
- A CDN can use **a unique npub/key per machine**, signed endpoint discovery,
  client/gateway selection, fallback, and cache-miss coalescing. Shared identity
  and network anycast are not prerequisites. See [architecture](docs/ARCHITECTURE.md).

## Contents and status

| Document | Status |
|---|---|
| [Evidence correction](docs/EVIDENCE-CORRECTION.md) | Authoritative interpretation and withdrawn claims |
| [Architecture](docs/ARCHITECTURE.md) | Conceptual unique-identity design, not yet implemented |
| [Report](REPORT.md) | Scope, offline verification and publication limitations |
| [Gap analysis](docs/GAP-ANALYSIS.md), [test plan](docs/E2E-TEST-PLAN.md) | Historical planning; correction takes precedence |
| [Runbook](docs/RUNBOOK.md) | Historical reference, not deployment approval |
| `evidence/` | Preserved dated observations and superseded interpretations |
| `scripts/`, `ansible/` | Prototype proxy, replication and deployment tooling |

The two `phase3*probe.sh` scripts are experimental and **not approved for rerun**;
see their warnings and the correction. They are not acceptance evidence. Local
HTTP tests exercise prototype mechanics against test servers, not fleet routing,
real Blossom interoperability, browser delivery or geographic selection.

## Original scope

The received proposal asked for four regional Blossom stores (US/EU/Asia/UK),
upload-only replication, unique regional FIPS identities and experimental cloned
sidecar identities. The historical fleet record did not establish four independent
regions. The shared-sidecar proposal is retained in historical docs for audit,
not endorsed as the current design. No live probes or deployments are part of
this correction.

## Verification and privacy

Run `make test` for local prototype tests; do not run phase3 probes. The repository
is private and contains historical operational network identifiers. Preserve
that visibility; a secret scan alone does not clear it for public release.
