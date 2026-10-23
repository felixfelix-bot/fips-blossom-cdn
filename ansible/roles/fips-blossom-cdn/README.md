# fips-blossom-cdn — per-region deployment role

Deploys one CDN region: a FIPS sidecar, a Blossom server, the HTTP proxy, and
the 60 s sync timer. Designed for the test plan in `docs/E2E-TEST-PLAN.md`.

**Idempotent by construction.** Every task is state-declaring; running the
playbook twice must report `changed=0` on the second pass apart from genuine
drift. Do not add `command:`/`shell:` tasks that are not guarded by a `creates:`
or a `changed_when:`.

**Never logs a key.** Any task touching the shared CDN identity carries
`no_log: true`, and the identity is delivered via `LoadCredential` — never as a
task argument and never rendered into a config that could be committed.

## Variables

| Variable | Default | Meaning |
|---|---|---|
| `region` | *required* | short region slug (`us`, `eu`, `asia`, `uk`) |
| `fips_version` | `0.5.3` | **pin**; upstream `master` is `0.6.0-dev` |
| `blossom_port` | `3000` | Blossom listen port (loopback only) |
| `proxy_port` | `3200` | HTTP proxy port |
| `sync_interval` | `60` | seconds between sync runs |
| `peers` | `[]` | list of peer proxy base URLs, e.g. `http://eu.fips:3200` |
| `public_ip` | `""` | this host's public IP; required if advertising TCP on nostr |
| `cdn_identity_file` | `""` | **path** to the shared CDN nsec on the controller; copied with 0600 and only via `no_log` |

## Two identity planes (see docs/GAP-ANALYSIS.md GAP-4)

- **Regional identity** — unique per host, persisted by the FIPS daemon. This is
  the **reliable** endpoint and what health checks target.
- **Shared CDN identity** — the *experimental* anycast plane. Its behaviour is
  unknown until Phase 3 of the test plan returns a verdict. **The role must not
  make anything else depend on it.**

## Files

- `tasks/main.yml` — orchestration
- `tasks/fips.yml` — sidecar + config
- `tasks/blossom.yml` — blob store + server
- `tasks/proxy.yml` — `/blossom/`, `/inventory`, `/sync-now`
- `tasks/sync.yml` — timer + unit with flock
- `tasks/firewall.yml` — loopback-only + fips0 ingress
- `tasks/health.yml` — health check script and unit
- `templates/` — units and configs
- `defaults/main.yml` — variables
- `handlers/main.yml` — restarts

## Rollback

See `docs/RUNBOOK.md` §4. The role deliberately does **not** implement a
destructive `state: absent` path for the blob store — data removal must be an
explicit human decision.
