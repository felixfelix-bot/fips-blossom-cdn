# Fleet CDN over FIPS — deployment and measured results

Status: **deployed and measured on four real hosts**.
Previous state of this project: one host, namespaces, injected latency, no
deployment. That gap is what this document closes.

## Topology

Four machines, two sites, one FIPS mesh.

| Inventory name | ssh alias    | Role    | Site | fips0 address                              |
|----------------|--------------|---------|------|--------------------------------------------|
| `hz-de-a`      | `dq05`       | origin  | DE   | `fdb0:bf34:d648:96ac:f65a:3fd6:358:710b`   |
| `hz-de-b`      | `cobrador`   | replica | DE   | `fd97:77d4:cd27:a6ae:1b29:e92e:fd96:dee8`   |
| `hz-us-a`      | `hermes-nvme`| replica | US   | `fdb8:d8ff:833c:6814:5865:55ac:3c98:afd9`   |
| `hz-us-b`      | `testserver2`| replica | US   | `fdfd:c0e5:3717:6cb1:bb60:de97:987e:7149`   |

Addresses are `fips0` mesh addresses, not LAN addresses. Every replica fills from
the origin over the mesh. Filling over the LAN would measure the LAN.

This is **two sites, not four**. `testserver2` and `vps2fips` are the same
machine, so four independent regions were never available. DE + US is a real
transatlantic path and is the honest maximum available on this fleet.

## Natural asymmetry, no netem

Measured from the manager (`cobrador`), `/health` round trip:

- `hz-de-a` (DE, same site): **8.9 ms**
- `hz-de-b` (DE, same site): **6.7 ms**
- `hz-us-a` (US): **658.7 ms**
- `hz-us-b` (US): **605.7 ms**

Roughly **90×** spread between the near and far replicas, and it is not
synthetic. The single-host demo needed `netem` to create a 60 ms difference;
here the difference is geography. An earlier RTT matrix put DE↔DE at 0.46 ms,
US↔US at 0.26 ms and DE↔US at 192–270 ms, so the spread is intrinsic to the
links rather than an artifact of the health endpoint.

## The blocker that was not a blocker

Phase 1.5 of this project concluded that "TCP does not work over FIPS" — ICMPv6
echo succeeded between mesh peers while TCP to a fresh port timed out. That
conclusion was **wrong**, and the way it was wrong matters.

`/etc/fips/fips.nft` installs an `inet fips` nftables table, entirely separate
from ufw. It default-DROPs inbound traffic on `fips0` and permits only:

- conntrack-established/related traffic
- ICMPv6 echo request
- a port allowlist (22, 53, 80, 443, 3001, 8200, 8443 in the observed config)

So `ping6` worked and TCP to port 31996 did not, and ufw reported
`-i fips0 -j ACCEPT` the whole time. Verifying the firewall and finding it
permissive tells you nothing when a second, unrelated ruleset is dropping the
packets afterwards. The evidence that settled it:

- `tcpdump -i fips0` on the receiver: SYN arrives, no SYN-ACK leaves
- SYN sent to allowlisted port 8443: `TCP_OK b'HELLO-PORT-8443'` in **511 ms**
- the `inet fips` chain carries its own counter: `counter packets 776 bytes 70577 drop`

Conclusion: **the FIPS data plane was never broken.** The CDN could always have
run on it; the port was simply not on the allowlist.

## Second bug, exposed by fixing the first

Once the port was allowed, connections changed from *timeout* to
*ConnectionRefused* — progress, and a new defect. The node bound `0.0.0.0`,
which is **IPv4 only**. `fips0` is IPv6 only, so an incoming SYN found no
matching socket and the kernel answered RST.

Fixed in `scripts/cdn_fleet_node.py` with a `DualStackServer` that binds `::`
with `IPV6_V6ONLY=0`, so one socket serves both families. Listener now shows
`*:18080` rather than `0.0.0.0:18080`.

Worth noting the failure mode: a wrong bind presents as a firewall problem.
RST from the kernel and a DROP rule look similar from a client that only reports
"connection failed".

## Reachability, measured over the mesh

`dq05` → replicas, using `fips0` addresses:

- `hz-de-b`: **20.8 ms**, HTTP 200
- `hz-us-a`: **578.5 ms**, HTTP 200
- `hz-us-b`: **602.8 ms**, HTTP 200

And from the manager to all four: 200 on every node.

## Deployment

`ansible/playbooks/cdn.yml` plus `ansible/inventory/fleet.yml`. Idempotent:
re-running reports `changed=0` when nothing has moved.

```
cd ansible
ansible-playbook -i inventory/fleet.yml playbooks/cdn.yml
```

Final run: `failed=0` on all four hosts, `unreachable=0`.

Design decisions worth recording:

- **One tarball, not per-file copies.** Per-file `copy` tasks over the
  multiplexed SSH link stalled for minutes on the high-latency hosts. One
  `tar czf` and one transfer is a single round trip.
- **No interpreter-probe task.** A `command: python3 -V` probe plus ansible's
  own interpreter discovery hung waiting on stdin. Both removed.
- **`become: true` at play level** for `/etc/systemd/system`.
- **`creates:` guards verified.** A stale guard silently skipped extraction,
  which would have kept old code on disk while reporting success.
- **tmpfs storage** (`/var/tmp/cdn-fleet`) because the fleet hosts are tight on
  disk. This is a test-only choice: a reboot loses the cache, which is correct
  for a cache and irrelevant to the claims.
- **Assert `/health`, not unit state.** During the crash loop (wrong
  `ExecStart` path, 96 restarts in 3 minutes) systemd reported `activating`,
  never `failed`. Asserting on unit state would have called that healthy.

## What is still open

- Throughput and eviction under real load on the fleet (`M4`, `M5`).
- Repeated runs. One measurement is an anecdote; the selector claim deserves
  several.
- Authorization. Any node will serve any key it has; there is no access control
  on the mesh. On a four-host private mesh that is acceptable, and it is still
  an absence.
- Range requests and resumable transfers. Not implemented.

## Reproduction

```
cd ~/worktrees/fips-fleet
python3 scripts/fleet_cdn_test.py \
  --node hz-de-a=http://[fdb0:bf34:d648:96ac:f65a:3fd6:358:710b]:18080 \
  --node hz-de-b=http://[fd97:77d4:cd27:a6ae:1b29:e92e:fd96:dee8]:18080 \
  --node hz-us-a=http://[fdb8:d8ff:833c:6814:5865:55ac:3c98:afd9]:18080 \
  --node hz-us-b=http://[fdfd:c0e5:3717:6cb1:bb60:de97:987e:7149]:18080 \
  --ssh hz-de-a=dq05 --ssh hz-de-b=cobrador \
  --ssh hz-us-a=hermes-nvme --ssh hz-us-b=testserver2 \
  --out evidence/fleet-cdn-results.json
```

The single-host namespace harness still exists and should still pass:

```
sudo make cdn-acceptance
```

If the fleet numbers and the namespace numbers disagree, the disagreement is
the finding. Do not average them.
