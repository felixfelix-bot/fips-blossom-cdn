# Runbook — deploy, verify, roll back

Assumes the ansible role shape in `ansible/roles/`. Every command is idempotent;
running the whole sequence twice must be a no-op on the second pass.

## 0. Prerequisites (per host)

```bash
uname -m                              # amd64 or arm64
ls -l /dev/net/tun                    # must exist
command -v docker || command -v podman
df -h /                               # need room for the blob store
ss -ltnH 'sport = :3000'              # must be empty (or reuse an existing blossom)
```

Install FIPS **v0.5.3** (do not track `master` — it is `0.6.0-dev`):

```bash
V=0.5.3
curl -L -o /tmp/fips.deb \
  "https://github.com/jmcorgan/fips/releases/download/v${V}/fips_${V}_amd64.deb"
dpkg -i /tmp/fips.deb
systemctl enable --now fips
fipsctl show status
```

## 1. Identity (two planes — see GAP-4)

- **Regional identity** (reliable, per region): unique nsec per host. Generated
  once, persisted, never regenerated:

  ```bash
  fipsctl keygen --stdout    # prints nsec + npub; store via secrets, never in git
  ```

- **Shared CDN identity** (experimental anycast plane): one nsec, injected to
  every region's *sidecar*.

Inject secrets, never bake them:

```ini
# /etc/systemd/system/<unit>.service.d/override.conf   (root, 0600)
[Service]
LoadCredential=fips-nsec:/etc/fips-cdn/nsec
```

or, for a container, an env file with `chmod 600`, referenced by
`--env-file` — never a build arg (build args land in image history).

## 2. Deploy

```bash
cd ansible
ansible-playbook -i inventory/hosts.yml playbooks/fips-blossom-cdn.yml \
  --limit <region-host> --check      # dry-run first
ansible-playbook -i inventory/hosts.yml playbooks/fips-blossom-cdn.yml \
  --limit <region-host>
```

Idempotence gate: run it a **second** time and require `changed=0` apart from
tasks that genuinely reconcile drift.

## 3. Verify

```bash
# local health
curl -sf http://127.0.0.1:3000/health | jq .
# expect: {"fips":"up","blossom":"up","last_sync":"<iso8601>"}

# FIPS plane
fipsctl show status            # transports: 2, session established
fipsctl show peers             # connectivity: connected on the mesh peers

# replication, on demand
curl -sf -X POST http://127.0.0.1:3000/sync-now | jq .
# expect: {"checked":N,"uploaded":M,"failed":0}

# blob round-trip
sha=$(sha256sum file.bin | cut -d' ' -f1)
curl -sf -X PUT --data-binary @file.bin http://127.0.0.1:3000/blossom/upload
curl -sf "http://127.0.0.1:3000/blossom/$sha" | sha256sum   # must match $sha
```

## 4. Rollback

Order matters — stop the replicated layer before the mesh layer, so no sync job
writes during teardown.

```bash
# 1. stop the sync timer (no more writes)
systemctl disable --now fips-cdn-sync.timer

# 2. stop the sidecar + proxy
systemctl disable --now fips-cdn-sidecar.service fips-cdn-proxy.service

# 3. stop blossom
docker rm -f blossom-region || systemctl disable --now blossom

# 4. LEAVE the FIPS host node running — it is shared infrastructure and other
#    work depends on it. Only stop it if this host is dedicated.

# 5. verify nothing of ours is still listening
ss -ltnH 'sport = :3000'
```

**Data is not deleted by rollback.** The blob store directory is left in place;
remove it explicitly and deliberately if that is wanted.

## 5. Known traps

| Symptom | Cause | Fix |
|---|---|---|
| `transports: 0` | container on Docker bridge NAT | `--network host` (required) |
| `fipsctl` permission denied | user not in `fips` group | `usermod -aG fips $USER`, re-login |
| `duplicate field peers` in config | appending a `peers:` block to a config that has one | edit via `yaml.safe_load`/`dump`, never append |
| sync job overlaps itself | no lock | `flock` around the sync script |
| a blob present on peer but missing locally, forever | peer pruned it, no re-upload trigger | treat peer-present/local-absent as an upload trigger (GAP-5) |
| health endpoint flaps | it checks the **shared** identity (experimental) | check the **regional** identity (GAP-4) |
| key regenerated after reboot | identity not persisted | `LoadCredential` from a persistent file (GAP-7) |
