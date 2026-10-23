# Evidence 01 — Blossom HTTP surface (what a replication job can actually rely on)

**Sources** (both local, HEAD-pinned, 2026-10-23):
- Spec: `github.com/hzrd149/blossom` @ `b5bd280` (2026-06-15), `buds/01.md`…`12.md`
- Reference impl: `github.com/hzrd149/blossom-server` @ `a492dc6` (2026-10-06)

> ⚠️ **Spec renumbering:** `/list` and `DELETE` now live in **BUD-12**, not
> BUD-02. BUD-02 `02.md:11` says so explicitly.

## 1. Endpoint list

| Method | Path | BUD | Auth (ref impl) | Handler |
|---|---|---|---|---|
| `GET` | `/<sha256>[.ext]` | BUD-01 `01.md:26` | optional (public) | `routes/blobs.ts:56` |
| `HEAD` | `/<sha256>[.ext]` | BUD-01 `01.md:70` | optional (public) | `routes/blobs.ts:56,120` |
| `PUT` | `/upload` | BUD-02 `02.md:39` | `t=upload`+`x`* | `routes/upload.ts:155` |
| `HEAD` | `/upload` | BUD-06 `06.md:7` | `t=upload`+`x`* | `routes/upload.ts:67` |
| `GET` | `/list/:pubkey` | BUD-12 `12.md:13` | optional*, **off by default** | `routes/list.ts:46` |
| `DELETE` | `/:sha256[.ext]` | BUD-12 `12.md:39` | `t=delete`+`x`* | `routes/delete.ts:40` |
| `PUT` | `/mirror` | BUD-04 `04.md:9` | `t=upload`+`x`* | `routes/mirror.ts:218` |
| `PUT`/`HEAD` | `/media` | BUD-05 | `t=media`+`x`* | `routes/media.ts:290,221` |
| `PUT` | `/report` | BUD-09 | none | `routes/report.ts:32` |

\* config-gated `requireAuth`. All endpoints MUST be at domain root (BUD-01
`01.md:24`). Errors are `text/plain` + advisory `X-Reason` (`01.md:20`), and
`X-Reason` **MUST NOT** be parsed for control flow (`02.md:69`).

## 2. Global blob listing — **DOES NOT EXIST**

BUD-12 `12.md:17`: *"/list/<pubkey> MUST return a JSON array of Blob Descriptor
objects that were uploaded by the specified pubkey"* — and `12.md:15` marks it
**"optional and unrecommended"**. The handler is strictly pubkey-scoped; the
pubkey is the only filter axis (`routes/list.ts:46,69`); the DB query is
`FROM blobs b JOIN owners o ON o.blob = b.sha256 WHERE o.pubkey = ?`
(`db/blobs.ts:203-208`). `list.enabled` defaults **false**
(`config/schema.ts:430-436`). The admin API has no JSON listing either — only
`DELETE /blobs/:sha256` and `DELETE /users/:pubkey`
(`routes/admin-router.tsx:121,141`); `listAllBlobs` (`db/blobs.ts:410`) is
SSR-only HTML.

**Consequence:** cross-region inventory works only if every blob has a known,
enumerable owner.

1. **BEST** — one shared uploader key `K` per region for replicated uploads,
   `list.enabled: true` + `list.allowListOthers: true`, then paginate
   `GET /list/K?limit=1000&cursor=<last sha256>` (BUD-12 `12.md:19`; impl caps
   limit at 1000, `db/blobs.ts:174`).
2. **Do not rely on anonymous uploads** — see §3.
3. **DB-side is the only true global view:** `SELECT sha256,size FROM blobs`
   against `data/sqlite.db`. Out-of-band, not the HTTP API.

`HEAD /<sha256>` remains the endorsed existence probe — BUD-06 `06.md:12`:
*"Clients that want to check whether a blob already exists on the server SHOULD
use `HEAD /<sha256>` from BUD-01"* — and it is a pure DB lookup
(`db/blobs.ts:71-73`).

## 3. Upload auth

Server verifies a **kind `24242`** Nostr event, base64url in
`Authorization: Nostr <b64>` (`middleware/auth.ts`):

- `kind === 24242` (`auth.ts:99`)
- `created_at` not >60 s in the future (`auth.ts:102`)
- **`expiration` tag mandatory**, unix-seconds, must be in the future → else 401
  (`auth.ts:109-128`)
- **`t` tag mandatory**, must equal the endpoint verb `get|upload|list|delete|media`
  (`auth.ts:130`; `requireAuth` `auth.ts:210-224` → 403 on mismatch)
- optional `server` tags: if present the domain must match (`auth.ts:137-147`)
- **`x` tag**: `requireXTag()` (`auth.ts:241-248`) → 403 if no `x` equals the
  blob hash. Required for `upload`/`delete`/`mirror`/`media` (`11.md:77-83`)
- signature verified last (`auth.ts:150`)

**Delegation: NO dedicated BUD.** BUD-11 has no delegation/capability chain —
the token is signed by the acting key itself. Practical equivalent: **one shared
keypair across all regions**; `server` tags may list multiple domains
(`11.md:25`), so a single token can be valid on all four.

**Open/anonymous mode: YES.** `upload.requireAuth` defaults `true` but `false`
accepts anonymous uploads (`config/schema.ts:101-107`; `upload.ts:164-176`).
⚠️ But the impl then stores the owner as the literal `"anonymous"`
(`upload.ts:443`), which fails the 64-hex pubkey check in `/list`
(`list.ts:27,70`) — **such blobs are permanently unenumerable over HTTP.**
Also `upload.enabled:false` = read-only server (`schema.ts:94-100`);
`upload.requirePubkeyInRule:true` = invite-only.

## 4. Dedup / idempotency — ✅ verified, and it is exactly what we need

Spec `02.md:46-47`: *"If the blob was newly stored, the endpoint MUST respond
with `201 Created` … If the blob already exists, the endpoint MUST respond with
`200 OK` and a Blob Descriptor"*.

Storage model = **one blob row + many owner rows** — per-(blob,pubkey)
descriptors:

```sql
CREATE TABLE blobs (sha256 TEXT(64) PRIMARY KEY, size, type, uploaded);
CREATE TABLE owners (blob REFERENCES blobs(sha256) ON DELETE CASCADE,
                     pubkey, PRIMARY KEY (blob, pubkey));
```

(`migrations/001_initial.sql:2-14`; both inserts `INSERT OR IGNORE`,
`db/blobs.ts:73,83`.)

Same-sha upload by a **different** pubkey (`upload.ts:269-313`): returns `200`
and **adds an owner row**, leaving `uploaded` as the **first**-upload timestamp.
`X-SHA-256` mismatch with the body → `409` (`upload.ts:385-387`).
`HEAD /upload` dedup branch → `200` + `X-Reason: Blob already exists (dedup)`
(`upload.ts:148-150`).

**Verdict:** upload-only, never-delete replication is idempotent and safe.
Dedup is hash-only, not pubkey-scoped — so a second region can "claim" ownership
of a blob it never physically had, which is exactly what makes
inventory-via-`/list` workable.

## 5. Mirror — BUD-04 exists, **unusable for a 127.0.0.1 topology**

BUD-04 `PUT /mirror` (`04.md:9-20`) takes `{"url": ".../<sha>.pdf"}` and is
literally the server-A→server-B replication flow (`04.md:57-64`). Impl re-hashes
the fetched blob and enforces the `x` tag (`mirror.ts:518-535`), adds an owner
row on dedup (`mirror.ts:551-554`).

**Blockers:**

- **SSRF guard rejects loopback:** `[a === 127, "loopback (127.0.0.0/8)"]`
  (`utils/ip-guard.ts:98`), plus `10/8`, `172.16/12`, `192.168/16`, `::1`, ULA,
  link-local. Region B **cannot** mirror from region A's `127.0.0.1:3000` or any
  private mesh address.
- Still needs the `x` tag ⇒ needs prior inventory ⇒ **does not replace
  discovery**.
- Needs a publicly resolvable origin.

## 6. GC / expiry — **on by default, 1-week TTL**

`main.ts:114-142` starts a recursive prune loop whenever
`storage.rules.length > 0 || storage.removeWhenNoOwners` — first run after 60 s,
then **every 30 s**. `prune.ts:150-190` deletes blobs whose **last access** (or
upload, if never accessed) exceeds the governing rule's expiration.

Defaults (`config/schema.ts:74-83`):

```
rules: [{ type: "image/*", expiration: "1 month" },
        { type: "video/*", expiration: "1 week" },
        { type: "*",       expiration: "1 week" }]
```

Controls: `storage.rules[].{type,expiration,pubkeys}` — **also the MIME
allowlist** (non-matching type → `415`; **`rules: []` rejects ALL uploads**
because `getFileRule` returns null, `prune/rules.ts:100-127`);
`storage.removeWhenNoOwners` (default `false`);
`prune.{initialDelayMs,intervalMs,batchSize}`. No byte quota, no TTL header, no
`Sunset` header (BUD-01 `01.md:101` optional). `maxSize` is per-request only.

**Useful side effect:** `GET`/`HEAD /<sha256>` refresh the LRU clock
(`touchBlob`, `routes/blobs.ts:84`) — a replica's existence probe keeps the
source alive.

## 7. Required config per region (consolidated)

```yaml
upload:
  requireAuth: false          # or use a shared key K (preferred, see §2)
  requirePubkeyInRule: false
storage:
  rules:
    - { type: "*", expiration: "100 years" }   # else 1-week TTL destroys content
  removeWhenNoOwners: false
list:
  enabled: true
  allowListOthers: true
delete:
  requireAuth: true           # ref impl ships false = anyone may delete anything
mirror:
  enabled: false              # unusable here (SSRF guard)
```

## Design verdict

| Claim | Verdict |
|---|---|
| Upload-only, never-delete is idempotent | ✅ VERIFIED-SPEC + SOURCE |
| `HEAD /<sha256>` is a valid existence probe | ✅ VERIFIED-SPEC + SOURCE |
| "Compare SHA-256 inventories" over HTTP | ⚠️ only with `list.enabled` + one shared uploader pubkey |
| Mirror for replication | ⚠️ SSRF blocks loopback/private; still needs prior inventory |
| Blobs persist forever | ❌ default 1-week/1-month LRU prune, 30 s loop |
