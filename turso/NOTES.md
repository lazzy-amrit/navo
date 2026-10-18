# turso/ — assumptions, decisions, open questions

The Turso layer of InSQL: customers sign up through our Auth, get *our* URLs and
*our* tokens, and run SQL through our gateway. Turso (org slug, platform token,
hostnames, Turso JWTs) stays entirely server-side.

## What was missing in the repo (and what I did)

The task said "reuse the existing user model, auth dependency and DB handle" —
none of them existed yet. Auth imports `Database.models` (`User`, `RefreshToken`,
`RateLimit`, `Otp`) but there was no `Database/` package, no app entry point, no
`requirements.txt`/`pyproject.toml` and no tests. I added the minimum:

| File | Why |
|---|---|
| `Database/__init__.py` | The shared Peewee `db` handle (SQLite, WAL) + `utcnow()`. |
| `Database/models.py` | Exactly the four models Auth imports, **plus** `User.plan` (price.md) and `User.is_admin` (admin routes). Nothing else in Auth was touched. |
| `main.py` | App factory + lifespan (table bootstrap, Turso clients, usage-sync loop). Registers Auth routers and ours. |
| `requirements.txt`, `pyproject.toml` | Deps and pytest/ruff config (none existed). |
| `tests/` | 114 unit/integration tests, Turso fully mocked. |

`turso/models.create_tables()` creates **all** tables (Auth's and ours) — it is
idempotent and runs at app startup.

## Environment variables

See `turso/.env.example`. Required: `TURSO_ORG`, `TURSO_PLATFORM_TOKEN`,
`TURSO_ENC_KEY` (Fernet), plus Auth's `SECRET_KEY`. Important optional:
`TURSO_GROUP` (default `default`), `TURSO_LOCATION` (default `aws-ap-south-1`),
`TURSO_MAX_DATABASES` (default 100), `TURSO_HTTP_TIMEOUT`, `PROXY_MAX_BODY_BYTES`,
`PROXY_MAX_CONCURRENT_PER_DB`, `PUBLIC_BASE_URL`, `USAGE_SYNC_INTERVAL_SECONDS`,
and `PLAN_*` overrides for the price.md numbers. Config fails fast at import with
a clear message if a required var is missing or the Fernet key is malformed.

## Reading of price.md (source of truth)

- Plans: `free` = 1 db / 60 MB / 5M reads / 75K writes per month,
  `pro` = 1 db / 200 MB / 10M reads / 400K writes. They live in
  `config.PLANS` (env-overridable, per price.md "should remain configurable").
- **Per-plan DB limit** counts the owner's databases in status
  `creating|active|suspended` (a `failed` create does not lock the slot; the
  customer can delete the failed row and retry).
- **Global capacity** (`active+creating|suspended >= TURSO_MAX_DATABASES`) raises
  `CapacityReached` → HTTP 503. price.md's "55 databases" comes from capping
  *users* (50 free + 5 pro); user-count capping belongs to plan/billing
  management and is **not** enforced here. `TURSO_MAX_DATABASES=100` is the
  hard infra limit from price.md's Turso capacity table.
- **Quota = current UTC calendar month** counters vs the owner's **current**
  plan (`User.plan`). `Database.plan` is only a snapshot for display. Storage is
  checked against `UsageCounter.storage_bytes` (synced from Turso) *and* enforced
  hard on Turso via `size_limit`.
- Rows "read" = rows scanned (what Turso reports as `rows_read`); `VACUUM` is
  not special-cased — nothing in the gateway blocks it, our write quota still
  applies (recorded as `rows_written`).

## Key design decisions

**Names.** The customer-visible name is our `alias` (validated 2–64 chars,
lowercase/dash/underscore, normalised to lowercase, unique per owner among
non-deleted rows via a *partial* SQLite index). The real Turso name is an
internal `d-<12 hex>` generated per database; databases can never be renamed
upstream, so `PATCH /databases/{id}` only changes the alias (no Turso call).
`slug` = alias (only used for display today; future host routing uses `db_id`).

**Tokens.** Customers get `dbc_<43 url-safe chars>`, stored as sha256 hex and
shown exactly once. Turso's `rotate` (kills ALL tokens of a db) is only used by
the admin `rotate-turso-credentials` endpoint, which wipes our stored Turso JWTs
so they are re-minted on the next request (brief downtime — see docstring).
`resolve_token` caches `(token, db)` for 30 s and **every** mutation (revoke,
suspend, delete) invalidates the cache immediately.

**Read-only enforcement** uses Turso's `authorization=read-only` JWT, never SQL
string parsing. Full-access and read-only JWTs are stored separately, encrypted
with Fernet (`turso_token_full_enc` / `turso_token_ro_enc`), minted lazily.

**Scrubbing.** Every gateway response gets `base_url = null`; error messages are
scrubbed of the db hostname, the internal Turso name and the Turso JWT, plus a
regex for any `*-*.turso.io` host. Customers never receive a Turso hostname.
Customer-facing service errors are wrapped (`ProvisioningFailed`,
`UpstreamError`…) so `turso_*` error codes only ever surface on `/admin/*`.

**Metering.** The proxy sums `rows_read`/`rows_written` from each pipeline
result and applies one atomic `UPDATE ... = col + n` per request, plus bumps
`last_used_at`, as fire-and-forget tasks (awaited in tests via
`proxy.drain_background()`). The periodic `usage_sync_loop` (lifespan task,
default every 300 s) overwrites counters from Turso's authoritative usage
endpoint and corrects drift; `queries` is ours only.

**Quota before upstream.** `check_quota` runs on every gateway request *before*
the Turso call, so customers get our `429 quota_exceeded` (with
`resource/used/details`), not Turso's `BLOCKED`. If Turso still answers
`BLOCKED`, that maps to the same 429.

**Batch transactions** are two-phase: `BEGIN` + statements (connection kept
alive via `baton`), then `COMMIT` or `ROLLBACK` on the same baton — a failed
statement can never commit partial work. Note Hrana has a 5 s transaction window
and 10 s idle connections; our two round-trips fit comfortably.

**Group.** Free plan allows one group, so everything uses `TURSO_GROUP`
(created on demand by `ensure_group()`). Archived-group detection is
best-effort (the OpenAPI `Group` schema exposes no `archived` flag): we unarchive
when a flag appears in the response, and `health()` always calls `ensure_group()`.

**Errors.** Our format everywhere we own: `{"error": {"code", "message",
"details?"}}` via a single `AppError` handler. Auth's `HTTPException`s keep
FastAPI's default `{"detail": ...}` shape — changing that would change Auth's
observable behaviour, which the brief forbids. Same for FastAPI's 422 body
validation errors on *customer* routes (the gateway converts its own).

**Sync/async.** Peewee is synchronous: plain `def` routes for DB-only work,
`asyncio.to_thread` inside the async gateway. Clients are module singletons
installed/teardown by the lifespan (`set_clients` / `reset_clients`).

## How to run

```bash
cd Backend
python -m venv .venv && .venv/bin/pip install -r requirements.txt
cp turso/.env.example .env         # fill in TURSO_* and SECRET_KEY
.venv/bin/uvicorn main:app --reload

.venv/bin/python -m pytest         # 114 tests, no network
.venv/bin/python -m ruff check .

TURSO_LIVE=1 .venv/bin/python scripts/turso_smoke.py   # hits real Turso
```

## Deliberately not built

- **Host-based routing** (`https://{db_id}.db.<our-domain>` + wildcard DNS) so
  unmodified official SDKs work against us. The gateway endpoints are already
  SDK-shaped (`/v2/pipeline`, same JSON), so this is a routing layer on top —
  no service changes needed later.
- Plan/subscription management (upgrading a user to `pro`), billing, invoices.
- User-count caps (50 free / 5 pro) — belongs to signup/plan management.
- WebSockets, streaming results, `/beta/listen`.
- Migrations (only idempotent `create_tables`), backups/PITR, seed-from-dump
  provisioning.
- Per-statement metering in the raw pipeline (we meter per request) and
  row accounting for `/dump` exports (Turso reports it via usage sync instead).
- Rate limiting on the gateway (only a per-db concurrency semaphore); the Auth
  layer rate-limits session endpoints already.

## Open questions / risks

1. **Concurrent JWT mints**: two first requests for the same db can both mint a
   Turso JWT (one wins the row update). Harmless (both valid) but wasteful;
   a per-db lock would fix it.
2. **Group archive flag**: if Turso's create-database call fails with an
   "archived" error and no flag is visible on GET, provisioning fails with
   `provisioning_failed` (row marked `failed`, retryable after deletion). Worth
   verifying against a live account with `TURSO_LIVE=1`.
3. **Storage enforcement**: `size_limit` is applied on Turso at create time;
   Turso's own behaviour when the limit is hit (block writes) is the hard stop,
   our quota check is the friendly stop.
4. **Usage sync lag**: counters are real-time for rows read/written but
   `storage_bytes` only updates on sync (or on demand via the admin
   usage-sync endpoint).
