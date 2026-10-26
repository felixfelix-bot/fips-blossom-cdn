# Running the CDN fleet as infrastructure-as-code

Everything needed to recreate the four-node deployment from scratch. Nothing in
this document was typed into a host by hand; if it is not in the repo, it did
not happen.

## Prerequisites

On the control node (a machine with `ansible`, currently `cobrador`):

- `ansible-playbook` ≥ 2.14
- ssh aliases for the fleet hosts in `~/.ssh/config`, resolving over IPv6 with
  the fleet key. The inventory addresses hosts by **alias**, never by IP, so
  this file can be published.
- passwordless sudo on every target (needed for `/etc/systemd/system` and for
  the nftables allowance)

On every target host:

- `python3` (3.13/3.14 observed)
- `fips0` up and peered
- `nft` (part of the FIPS install)

## Apply

```
cd ansible
ansible-playbook -i inventory/fleet.yml playbooks/cdn.yml
```

Verify without changing anything:

```
ansible-playbook -i inventory/fleet.yml playbooks/cdn.yml --check --diff
```

Tear down:

```
ansible-playbook -i inventory/fleet.yml playbooks/cdn.yml -e cdn_state=absent
```

## What the playbook does, in order

1. **Gather facts** with `become: true` (needed later for `/etc` writes).
2. **Decide role and arguments.** Origin vs replica is derived from group
   membership in the inventory, not from a hardcoded hostname list. Replicas
   receive `--origin-url http://[<origin mesh ip>]:<port>`; the origin gets
   `--loopback`.
3. **Create the root and data directories** under `cdn_root` (default
   `/var/tmp/cdn-fleet`, tmpfs).
4. **Transfer one tarball** (hash-named so a change is visible in `changed`).
   One round trip instead of N file copies.
5. **Extract** into the CDN root, guarded so a stale tree is replaced rather
   than silently reused.
6. **Allow the CDN port through the FIPS nftables table** on `fips0`. This is
   the step whose absence produced a false "FIPS data plane is broken"
   conclusion; see `09-fleet-multihost.md`.
7. **Persist that allowance** across FIPS restarts, so a fleet reboot does not
   silently re-break the mesh.
8. **Render, enable and start** `cdn-node.service`.
9. **Assert `/health` returns 200** — not "the unit is active". A wrong
   `ExecStart` path produced 96 restarts in 3 minutes while systemd reported
   `activating`, not `failed`.
10. **Report stats** per node so the run ends with evidence, not a green tick.

## Why the tarball

The first working version used several `copy:` tasks. Over the multiplexed ssh
link to the US hosts each file copy stalled for tens of seconds and the play
appeared to hang. Packaging once turns N slow round trips into one, and makes
the deploy unit the same artifact the tests exercise.

## Why the nftables step is in the playbook

`/etc/fips/fips.nft` owns an `inet fips` table independent of ufw, default-DROP
inbound on `fips0`, allowing only established traffic, ICMPv6 echo and an
explicit port allowlist. A CDN node on an arbitrary port is invisible until its
port is added — and from the client it looks identical to a broken mesh.

Putting this in the playbook rather than in a runbook means a rebuilt host is
reachable over the mesh without anyone remembering the trick.

## Configuration surface

Inventory variables (`ansible/inventory/fleet.yml`):

- `cdn_port` — listen port, default `18080`
- `cdn_budget_bytes` — replica byte budget, default 256 MiB
- `cdn_entry_max_bytes` — largest single object, default 8 MiB
- `cdn_root` — install root, default `/var/tmp/cdn-fleet`
- `cdn_mesh_ip` — per-host `fips0` address, used to build the origin URL

Changing the origin host requires editing only group membership; the replica
URLs follow from `groups['cdn_origin'][0]`.

## Known sharp edges

- **tmpfs storage.** `/var/tmp/cdn-fleet` is lost on reboot. Correct for a
  cache, and it means "the node is up but the cache is cold" after any restart.
- **`testserver2` and `vps2fips` are the same machine.** Do not count them as
  two sites.
- **IPv6 literals in URLs need brackets.** `http://fd00::1:18080` is ambiguous
  and rejected by urllib; `http://[fd00::1]:18080` is correct.
- **A wrong bind looks like a firewall.** `0.0.0.0` is IPv4 only; on an IPv6
  mesh it yields RST, not a timeout. The node binds `::` with
  `IPV6_V6ONLY=0`.
