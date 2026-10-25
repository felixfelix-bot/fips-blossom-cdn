# Conceptual CDN over FIPS: one identity per machine

**Design proposal, not an implemented or measured CDN.** The authoritative
[evidence correction](EVIDENCE-CORRECTION.md) replaces the earlier claim that
shared-identity failure removes the CDN premise. Existing proxy/replication code
is a prototype; it does not implement the complete design below.

## Identity, object naming and discovery

- Each machine owns a unique FIPS npub/private key. Never clone transport keys
  across replicas. A node identity locates a machine; SHA-256 names object bytes.
- A trusted, signed endpoint registry maps object hashes (or inventory manifests)
  to candidate unique node identities and HTTP endpoints. Include issuer,
  issuance/expiry, sequence/version, supported access modes and availability
  leases. Validate signatures against configured trust anchors, freshness and
  replay rules. A signature proves issuer authorization, not actual availability.
- Separate node transport keys, registry signing authority and upload credentials.
  Discovery is not authorization. Enforce access controls, upload permissions,
  rate limits and registry endpoint/SSRF policy independently.

## Request path

1. A mesh-aware client or local gateway resolves a hash to authorized, unexpired
   candidate holders. Cache discovery only until expiry; refresh stale records.
2. Probe a bounded candidate set for health, availability and observed RTT/TTFB.
   Select a healthy holder using measured service quality rather than country
   labels; bound timeouts and concurrency, and use hysteresis to avoid flapping.
3. Fetch from that edge. On failure, corruption, 404 or stale lease, try another
   authorized holder, then origin/shield as policy permits. Record failures and
   refresh discovery; do not assume a signed lease guarantees bytes still exist.
4. Ordinary browsers need mesh access or a public HTTPS gateway. A public gateway
   can perform selection internally; redirecting to a mesh-only address does not
   make it reachable. Browser TLS, CORS and range behaviour need explicit tests.

## Edge cache and origin protection

On hit, serve the stored verified object. On miss, an edge fetches from an
allowlisted origin or shield. Coalesce concurrent misses **per hash** so waiters
share one fetch; bound waiters, fetch size/time, concurrent jobs and disk writes.
Use bounded temporary storage and atomic publication only after size and hash
validation. Clean incomplete files on failure; do not mark partial downloads as
verified. A shield can coalesce misses across multiple edges. This is an
application feature, independent of FIPS multicast or path multiplicity.

Whole-object hashes do not authenticate individual ranges before the whole
object is available. Range/streaming integrity needs an authenticated chunk/Merkle
manifest or a clearly documented whole-object verification policy. Hashes also
do not grant viewing rights, prove publisher intent, or revoke copies.

## Storage and replication

Use TTL/LRU eviction with explicit byte quotas and reserved headroom for edge
caches. Separate expendable caches from durable origin/replica commitments.
Availability leases expire or are withdrawn on eviction, and clients must tolerate
lag. Replication policy specifies minimum durable copies, independent failure
domains, retention and repair. Upload-only synchronization does not mean every
cache must store every object forever; reconcile storage expiry with promised
leases. Blossom inventories are per uploader unless a separate index supplies the
contract. Upload authorization and storage owner metadata remain distinct from
FIPS node identity.

## Capacity and evidence needed

Independent edge egress/compute can increase aggregate service capacity and cache
hits reduce origin traffic. Gains are bounded by shared links/min-cuts, CPU,
storage, bottleneck gateways and hot-object distribution. N replicas do not imply
N times throughput; the historical ~83 Mbit/s number is not a universal ceiling.
Replicas can improve latency and availability below an origin throughput ceiling,
so there is no universal viewer-count threshold below which they are useless.

Acceptance must cover trusted discovery and expiry/replay rejection; measured
selection under reversible latency/load asymmetry; bounded failure fallback;
concurrent cache misses with origin-fetch counts; corrupt/partial objects;
quota/eviction/lease consistency; browser access; and throughput over shared vs
independent bottlenecks. Simulated topology tests prove mechanics, not four-region
geographic performance. No such end-to-end acceptance is claimed by this PR.
