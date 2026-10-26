# Hashtree gap analysis — is it useful for this CDN?

Reviewed upstream at commit `abaaf6975b` (MIT, Rust + TypeScript).
Three independent reviews: build-vs-adopt, transport risk, security model.

## Verdict

**Useful as a reference implementation and a candidate backend. Not a drop-in
replacement, and not a fix for our open problem.**

The single most important finding is not about features. Hashtree does **not**
run its blob traffic over kernel TCP on `fips0` the way we do. It has its own
`FipsTcpEndpoint` / `fips-tcp` actor. That means our measured stall may be a
property of *our* stack — Python `ThreadingHTTPServer` over the fips0 TUN — and
not of the FIPS mesh at all.

**Update: it is not our stack.** Measured after writing this:

- `curl` (a C client) reproduces the failure exactly — 6/16, `200` with zero
  bytes for the other ten. The Python client is not implicated.
- The replica's `fips0` transmitted ~2 MB during the run with **0 TX drops and
  0 errors**, so the server handed the data to the mesh cleanly.
- The ceiling is exactly **6 concurrent bulk flows** (N=2/4/6 all succeed, N=8
  gives 6, N=12 gives 6, N=16 gives 6) and is deterministic across runs.
- On the LAN the same test gives 15/16.

So the bytes leave our stack correctly and still fail to arrive, and the bound
tracks path latency rather than anything we control. The remaining suspects are
inside the FIPS daemon's own per-flow handling. What hashtree buys is therefore
narrower than first framed: unless its transport bypasses kernel TCP on `fips0`
entirely, it would meet the same ceiling.

See `evidence/11-chunked-transfer.md` for the numbers and the chunked workaround
that removes the symptom without raising the ceiling.

## Where the systems actually differ

| Capability | Ours | Hashtree | Does it change what we can build? |
|---|---|---|---|
| Addressing | Whole-object SHA-256 | SHA-256 blobs + deterministic Merkle trees | Yes: directory snapshots, chunk reuse, partial reads |
| Partial reads | None | Range reads over chunk traversal | Yes, but note the **FIPS blob protocol still sends whole blobs** — not session resume |
| Encryption | FIPS transport only | CHK convergent encryption, keyed CIDs | Yes for storage isolation; see caveat below |
| Mutable naming | None | Signed Nostr roots, `npub/tree/path` | Yes: content can change without changing the address |
| Peer forwarding | Client→replica only | HTL-bounded forwarding + coalescing | Yes: replicas can source through peers |
| Selection policy | Measured fastest replica, cooldown, bounded failover | Adaptive `BlobRouter`, provider racing | **Different policy, not a better one** |
| Storage | Memory LRU + budgeted disk LRU | LMDB `PoolStore`, pins, relocation | More lifecycle, different eviction semantics |
| Blob ceiling | Whole objects, no cap | FIPS replies capped at 16 MiB, 2 MiB chunks | Yes: large objects need chunking |

## The overlap means our evidence does not transfer

We built singleflight coalescing, hash-verified-before-cache, and
measured-fastest selection. Hashtree has equivalents. But equivalents are not
the same policy, and our acceptance evidence (M1–M6) is evidence about **our**
policy.

In particular M2 — "selection follows measured latency, not configuration
order" — is a claim about `cdn/catalogue.py` and `cdn/selector.py`. Hashtree's
outer router learns *composite route* outcomes, not each FIPS peer's latency.
Adopting it does not carry M2 across; it discards M2 and starts again.

That is the real cost of adoption, and it is larger than the code we would
delete.

## The decisive arithmetic

If hashtree ran over the path as we measured it, it would not work at all:

- a 2 MiB chunk against a ~19,556-byte stall needs ~107 slices
- hashtree's protocol has **no offset resume** — reopening a blob sends the
  hash and restarts at byte zero
- one retry = two repeated prefixes = ~39,112 bytes received, **0 accepted**
- a permanently stalling path yields **zero completed blobs at any retry count**

Its documented "one bounded whole-session retry" retries *connection
establishment*, not stalled bodies. The TypeScript path makes two whole-session
rounds. Neither resumes a partial blob.

So on the path we measured, hashtree is not slower — it is non-functional. The
question is therefore binary and empirical: **does their native transport hit
the same stall?**

Their per-actor bounds (4 outbound requests, 8 inbound sessions, 32 connection
slots, 5.5 s idle timeout) also mean "128 chunks" never means 128 simultaneous
connections, which is a point in favour of their design under our failure mode.

## Security: a correction to what I said earlier

I described CHK as "encrypted by default, still dedups", implying
confidentiality. That was too generous.

- Mechanism: `H = SHA256(plaintext)`, `K = HKDF-SHA256(H, salt="hashtree-chk")`,
  `AES-256-GCM(K, zero-nonce, plaintext)`, CID carries `SHA256(ciphertext)` and
  the key.
- **Public root events publish the key** (`["key", …]`). Any storage operator
  can fetch it and decrypt. "Encrypted by default" is **not** "confidential by
  default".
- No per-user salt: identical plaintext produces globally identical ciphertext.
  That enables confirmation-of-file and offline dictionary attacks, and
  cross-user equality correlation.
- Serving peers still see requested hashes, timing, sizes and repetition.
- Signed roots give **local** monotonicity (newest `created_at`, lowest event ID
  on ties) but no global freshness. A fresh client can accept an old valid root
  if relays suppress newer ones. No expiry, no externally anchored sequence.

For a private four-host fleet this is a modest gain. For public Blossom storage
of predictable content it is a real hazard, and the right answer there is
client-side encryption *without* global dedup, not CHK.

## Recommendation

1. **Do not migrate now.** The stall is unresolved; swapping the storage layer
   adds a variable instead of removing one, and would invalidate M1–M6.
2. **Run one decisive experiment.** Install hashtree's native FIPS transport on
   the same two hosts across the same DE↔US path and run our existing M6 shape
   against it: 16 concurrent fetches of the same blob. Pass = 16/16 complete.
   - 16/16 → the stall is ours; fix our transport, and hashtree becomes a
     credible backend.
   - same ~20 KB stall → it is the mesh; stop blaming our code, and hashtree is
     not usable here until that is fixed.
3. **Borrow two ideas regardless of adoption.** Chunking with per-chunk
   verification (bounds retry cost and enables resume — it converts our
   unrecoverable stall into a retry), and CHK-style encryption if we ever store
   on hosts we do not own.
4. **Keep `ansible/` and the test suite.** Fleet operations and acceptance
   gates survive any application swap; that is where the durable value is.

## What would make me change my mind

If the native-transport test passes 16/16 *and* someone needs directory
snapshots, mutable names, or git-over-mesh, then adopting behind a
`get/put` boundary is worth it. Without a demonstrated fix for the stall,
everything else is a feature list for a system that cannot transfer a file.
