"""All business logic for "the database company".

Every function takes the acting user and checks ownership; admin functions
check the admin flag. Rules come from price.md (plans, quotas, capacity).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from datetime import timedelta

from Database import utcnow
from Database.models import User
from turso import config, crypto
from turso.errors import (
    AliasInvalid,
    AliasTaken,
    AppError,
    CapacityReached,
    DatabaseUnavailable,
    ForbiddenError,
    NotFoundError,
    PlanLimitReached,
    ProvisioningFailed,
    QuotaExceeded,
    TokenInvalid,
    TursoError,
    TursoNotFound,
    UpstreamError,
    ValidationError,
)
from turso.models import (
    PERMISSIONS,
    ApiToken,
    AuditLog,
    Database,
    UsageCounter,
    new_database_id,
    new_turso_name,
)
from turso.turso import get_platform_client

logger = logging.getLogger("turso.service")

ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$")
COUNTED_STATUSES = ("creating", "active", "suspended")

_token_cache: dict[str, tuple[ApiToken, Database, float]] = {}
_token_cache_lock = threading.Lock()
TOKEN_CACHE_TTL = 30.0


# --- small helpers ---------------------------------------------------------


def current_period(now=None) -> str:
    return (now or utcnow()).strftime("%Y-%m")


def connection_url(db: Database) -> str:
    """Our public URL for the database - never Turso's hostname."""
    return f"{config.PUBLIC_BASE_URL}/v1/db/{db.id}"


def connection_info(db: Database) -> dict:
    return {
        "database_id": db.id,
        "url": connection_url(db),
        "pipeline_path": f"/v1/db/{db.id}/v2/pipeline",
        "query_path": f"/v1/db/{db.id}/query",
        "health_path": f"/v1/db/{db.id}/health",
        "auth_header": "Authorization: Bearer <your InSQL token>",
    }


def validate_alias(alias: str) -> str:
    alias = (alias or "").strip().lower()
    if not ALIAS_RE.fullmatch(alias):
        raise AliasInvalid(
            "Alias must be 2-64 characters, lowercase letters, digits, dashes or "
            "underscores, starting and ending with a letter or digit."
        )
    return alias


def _plan_limits(user: User) -> config.PlanLimits:
    return config.plan_limits(user.plan or config.DEFAULT_PLAN)


def _require_admin(user: User) -> None:
    if not getattr(user, "is_admin", False):
        raise ForbiddenError("Admin access required")


def _owned_database(user: User, db_id: str) -> Database:
    db = Database.get_or_none(Database.id == db_id)
    if db is None or db.deleted_at is not None or db.owner_id != user.id:
        raise NotFoundError("Database not found")
    return db


def _admin_database(db_id: str) -> Database:
    db = Database.get_or_none(Database.id == db_id)
    if db is None or db.deleted_at is not None:
        raise NotFoundError("Database not found")
    return db


def _audit(action: str, *, owner: User | None = None, database: Database | None = None,
           metadata: dict | None = None, ip: str | None = None) -> None:
    AuditLog.create(
        owner=owner,
        database=database,
        action=action,
        metadata=json.dumps(metadata or {}, default=str),
        ip=ip,
    )


# --- databases -------------------------------------------------------------


def create_database(
    user: User,
    alias: str,
    region: str | None = None,
    size_limit: int | None = None,
    create_initial_token: bool = False,
    ip: str | None = None,
) -> tuple[Database, dict, tuple[ApiToken, str] | None]:
    """Provision a customer database on Turso and record it in our metadata DB.

    Returns ``(db, connection_info, initial_token)`` where ``initial_token`` is
    ``(token_row, raw_token)`` only when ``create_initial_token`` is set.
    """
    alias = validate_alias(alias)
    plan = _plan_limits(user)

    existing = (
        Database.select()
        .where((Database.owner == user) & (Database.alias == alias) & (Database.deleted_at.is_null()))
        .exists()
    )
    if existing:
        raise AliasTaken("You already have a database with this alias")

    if _count_databases(user) >= plan.databases:
        raise PlanLimitReached(
            f"Your plan allows {plan.databases} database(s). Delete one or upgrade your plan."
        )
    if _capacity_used() >= config.TURSO_MAX_DATABASES:
        raise CapacityReached(
            "We are at capacity right now. Please try again in a little while."
        )

    if region is not None and region != config.TURSO_LOCATION:
        raise ValidationError(
            f"Region '{region}' is not available. Available regions: {config.TURSO_LOCATION}"
        )
    if size_limit is None:
        size_limit = plan.storage_bytes
    elif size_limit <= 0:
        raise ValidationError("size_limit must be a positive number of bytes")
    elif size_limit > plan.storage_bytes:
        raise ValidationError(
            f"size_limit exceeds your plan's storage quota of {plan.storage_mb} MB"
        )

    db = Database.create(
        id=new_database_id(),
        owner=user,
        alias=alias,
        slug=alias,
        turso_name=new_turso_name(),
        turso_group=config.TURSO_GROUP,
        region=region or config.TURSO_LOCATION,
        status="creating",
        plan=user.plan or config.DEFAULT_PLAN,
        size_limit_bytes=size_limit,
    )

    platform = get_platform_client()
    try:
        platform.ensure_group()
        created = platform.create_database(db.turso_name, group=db.turso_group)
        db.turso_hostname = created.get("Hostname") or f"{db.turso_name}-{platform.org}.turso.io"
        db.status = "active"
        db.save()
    except AppError as exc:
        db.status = "failed"
        db.status_reason = str(exc)[:500]
        db.save()
        _audit("database.create_failed", owner=user, database=db,
               metadata={"error": str(exc)}, ip=ip)
        raise ProvisioningFailed(
            "Could not provision the database. It is marked as failed; "
            "you can delete it and try again."
        ) from exc

    try:
        platform.update_db_config(db.turso_name, size_limit=str(size_limit))
    except AppError as exc:
        # The database exists and works; our own quota still protects storage.
        db.status_reason = f"size limit not applied: {exc}"[:500]
        db.save()
        logger.warning("size limit not applied for %s: %s", db.turso_name, exc)

    _audit("database.create", owner=user, database=db,
           metadata={"alias": alias, "region": db.region, "size_limit_bytes": size_limit}, ip=ip)

    initial_token = None
    if create_initial_token:
        initial_token = create_token(user, db.id, "default", "full-access", ip=ip)

    return db, connection_info(db), initial_token


def _count_databases(user: User) -> int:
    return (
        Database.select()
        .where((Database.owner == user) & (Database.status.in_(COUNTED_STATUSES)))
        .count()
    )


def _capacity_used() -> int:
    return Database.select().where(Database.status.in_(COUNTED_STATUSES)).count()


def list_databases(user: User) -> list[Database]:
    return list(
        Database.select()
        .where((Database.owner == user) & (Database.deleted_at.is_null()))
        .order_by(Database.created_at.desc())
    )


def get_database(user: User, db_id: str) -> Database:
    return _owned_database(user, db_id)


def rename_database(user: User, db_id: str, new_alias: str) -> Database:
    """Alias-only rename. Databases cannot be renamed on Turso."""
    db = _owned_database(user, db_id)
    new_alias = validate_alias(new_alias)
    if new_alias == db.alias:
        return db
    taken = (
        Database.select()
        .where(
            (Database.owner == user)
            & (Database.alias == new_alias)
            & (Database.deleted_at.is_null())
            & (Database.id != db.id)
        )
        .exists()
    )
    if taken:
        raise AliasTaken("You already have a database with this alias")
    old = db.alias
    db.alias = new_alias
    db.slug = new_alias
    db.save()
    _audit("database.rename", owner=user, database=db, metadata={"from": old, "to": new_alias})
    return db


def delete_database(user: User, db_id: str, ip: str | None = None) -> None:
    """Revoke our tokens, delete on Turso, mark deleted. Safe to retry."""
    db = _owned_database(user, db_id)
    if db.status == "deleted":
        raise NotFoundError("Database not found")

    db.status = "deleting"
    db.save()
    revoke_all_tokens(user, db_id)

    try:
        get_platform_client().delete_database(db.turso_name)
    except TursoNotFound:
        pass  # already gone upstream (or never created) - success
    except TursoError as exc:
        raise UpstreamError(
            "Could not delete the database upstream. It is marked as deleting; "
            "please retry."
        ) from exc

    db.status = "deleted"
    db.deleted_at = utcnow()
    db.save()
    _invalidate_token_cache(db_id=db.id)
    _audit("database.delete", owner=user, database=db, ip=ip)


def suspend_database(user: User, db_id: str) -> Database:
    """Admin: block reads and writes on Turso."""
    _require_admin(user)
    db = _admin_database(db_id)
    if db.status not in ("active", "suspended"):
        raise DatabaseUnavailable(f"Database is {db.status} and cannot be suspended")
    try:
        get_platform_client().update_db_config(
            db.turso_name, block_reads=True, block_writes=True
        )
    except TursoError as exc:
        raise UpstreamError("Could not suspend the database") from exc
    db.status = "suspended"
    db.save()
    _invalidate_token_cache(db_id=db.id)
    _audit("database.suspend", owner=db.owner, database=db)
    return db


def resume_database(user: User, db_id: str) -> Database:
    """Admin: unblock reads and writes on Turso."""
    _require_admin(user)
    db = _admin_database(db_id)
    if db.status not in ("active", "suspended"):
        raise DatabaseUnavailable(f"Database is {db.status} and cannot be resumed")
    try:
        get_platform_client().update_db_config(
            db.turso_name, block_reads=False, block_writes=False
        )
    except TursoError as exc:
        raise UpstreamError("Could not resume the database") from exc
    db.status = "active"
    db.save()
    _invalidate_token_cache(db_id=db.id)
    _audit("database.resume", owner=db.owner, database=db)
    return db


def get_connection_info(user: User, db_id: str) -> dict:
    db = _owned_database(user, db_id)
    return connection_info(db)


# --- our API tokens --------------------------------------------------------


def create_token(
    user: User,
    db_id: str,
    name: str,
    permission: str = "full-access",
    expires_in: int | None = None,
    ip: str | None = None,
) -> tuple[ApiToken, str]:
    """Mint one of our tokens. The raw value is returned exactly once."""
    db = _owned_database(user, db_id)
    if db.status != "active":
        raise DatabaseUnavailable(f"Database is {db.status}")
    if permission not in PERMISSIONS:
        raise ValidationError(f"permission must be one of: {', '.join(PERMISSIONS)}")
    name = (name or "").strip()
    if not name:
        raise ValidationError("Token name is required")
    expires_at = None
    if expires_in is not None:
        if expires_in <= 0:
            raise ValidationError("expires_in must be a positive number of seconds")
        expires_at = utcnow() + timedelta(seconds=expires_in)

    raw = crypto.generate_api_token()
    token = ApiToken.create(
        database=db,
        name=name[:80],
        prefix=crypto.token_prefix(raw),
        token_hash=crypto.hash_token(raw),
        permission=permission,
        expires_at=expires_at,
    )
    _audit("token.create", owner=user, database=db,
           metadata={"token_id": token.id, "permission": permission}, ip=ip)
    return token, raw


def list_tokens(user: User, db_id: str) -> list[ApiToken]:
    db = _owned_database(user, db_id)
    return list(
        ApiToken.select()
        .where(ApiToken.database == db)
        .order_by(ApiToken.created_at.desc())
    )


def revoke_token(user: User, db_id: str, token_id: int) -> None:
    db = _owned_database(user, db_id)
    token = ApiToken.get_or_none(ApiToken.id == token_id)
    if token is None or token.database_id != db.id:
        raise NotFoundError("Token not found")
    if token.revoked_at is None:
        token.revoked_at = utcnow()
        token.save()
        _invalidate_token_cache(token_hash=token.token_hash)
        _audit("token.revoke", owner=user, database=db, metadata={"token_id": token.id})


def revoke_all_tokens(user: User, db_id: str) -> int:
    db = _owned_database(user, db_id)
    count = (
        ApiToken.update(revoked_at=utcnow())
        .where((ApiToken.database == db) & (ApiToken.revoked_at.is_null()))
        .execute()
    )
    _invalidate_token_cache(db_id=db.id)
    if count:
        _audit("token.revoke_all", owner=user, database=db, metadata={"count": count})
    return count


def resolve_token(raw_token: str) -> tuple[ApiToken, Database]:
    """Proxy entry point: our raw token -> (token, database) or a typed error."""
    if not raw_token or not raw_token.startswith(f"{crypto.TOKEN_PREFIX}_"):
        raise TokenInvalid("Invalid or expired token")
    digest = crypto.hash_token(raw_token)

    cached = _cache_get(digest)
    if cached is not None:
        token, db = cached
    else:
        token = ApiToken.get_or_none(ApiToken.token_hash == digest)
        if token is None or not crypto.tokens_equal(token.token_hash, digest):
            raise TokenInvalid("Invalid or expired token")
        db = token.database
        _cache_put(digest, token, db)

    if token.revoked_at is not None:
        raise TokenInvalid("Invalid or expired token")
    if token.expires_at is not None and token.expires_at <= utcnow():
        raise TokenInvalid("Invalid or expired token")
    if db.status == "suspended":
        raise DatabaseUnavailable("Database is suspended", code="database_suspended")
    if db.status != "active":
        raise DatabaseUnavailable(f"Database is {db.status}")
    return token, db


def touch_token(token: ApiToken) -> None:
    ApiToken.update(last_used_at=utcnow()).where(ApiToken.id == token.id).execute()


def _cache_get(digest: str) -> tuple[ApiToken, Database] | None:
    with _token_cache_lock:
        entry = _token_cache.get(digest)
        if entry is None:
            return None
        token, db, expires = entry
        if time.monotonic() > expires:
            _token_cache.pop(digest, None)
            return None
        return token, db


def _cache_put(digest: str, token: ApiToken, db: Database) -> None:
    with _token_cache_lock:
        _token_cache[digest] = (token, db, time.monotonic() + TOKEN_CACHE_TTL)


def _invalidate_token_cache(*, token_hash: str | None = None, db_id: str | None = None) -> None:
    with _token_cache_lock:
        if token_hash is not None:
            _token_cache.pop(token_hash, None)
        elif db_id is not None:
            for key in [k for k, v in _token_cache.items() if v[1].id == db_id]:
                _token_cache.pop(key, None)
        else:
            _token_cache.clear()


# --- Turso-side credentials ------------------------------------------------


def get_turso_jwt(db: Database, permission: str = "full-access") -> str:
    """Lazily mint and cache the Turso JWT for this database.

    Read-only enforcement uses Turso's read-only JWT, never SQL parsing.
    """
    field = "turso_token_full_enc" if permission != "read-only" else "turso_token_ro_enc"
    stored = getattr(db, field)
    if stored:
        try:
            return crypto.decrypt_secret(stored)
        except Exception:  # key rotation / corrupted value -> re-mint
            logger.warning("stored turso token unreadable for %s, re-minting", db.id)

    jwt = get_platform_client().create_db_token(
        db.turso_name, authorization=permission, expiration="never"
    )
    encrypted = crypto.encrypt_secret(jwt)
    Database.update({getattr(Database, field): encrypted}).where(Database.id == db.id).execute()
    setattr(db, field, encrypted)
    return jwt


def refresh_turso_jwt(db: Database, permission: str = "full-access") -> str:
    """Drop the stored Turso JWT (suspected invalidation) and mint a new one."""
    field = "turso_token_full_enc" if permission != "read-only" else "turso_token_ro_enc"
    Database.update({getattr(Database, field): None}).where(Database.id == db.id).execute()
    setattr(db, field, None)
    return get_turso_jwt(db, permission)


def rotate_turso_credentials(user: User, db_id: str) -> None:
    """EMERGENCY: invalidate ALL Turso tokens for this database.

    Causes brief downtime: in-flight queries fail until the next request
    re-mints a fresh Turso JWT.
    """
    _require_admin(user)
    db = _admin_database(db_id)
    try:
        get_platform_client().rotate_db_tokens(db.turso_name)
    except TursoError as exc:
        raise UpstreamError("Could not rotate Turso credentials") from exc
    Database.update({Database.turso_token_full_enc: None, Database.turso_token_ro_enc: None}) \
        .where(Database.id == db.id).execute()
    db.turso_token_full_enc = None
    db.turso_token_ro_enc = None
    _invalidate_token_cache(db_id=db.id)
    _audit("database.rotate_credentials", owner=user, database=db)


# --- usage and quotas ------------------------------------------------------


def get_usage(
    user: User, db_id: str, from_: str | None = None, to: str | None = None
) -> dict:
    db = _owned_database(user, db_id)
    try:
        data = get_platform_client().get_db_usage(db.turso_name, from_=from_, to=to)
    except TursoError as exc:
        raise UpstreamError("Could not fetch usage right now") from exc
    total = data.get("total", {}) or {}
    return {
        "database_id": db.id,
        "from": from_,
        "to": to,
        "rows_read": total.get("rows_read", 0),
        "rows_written": total.get("rows_written", 0),
        "storage_bytes": total.get("storage_bytes", 0),
        "bytes_synced": total.get("bytes_synced", 0),
        "instances": data.get("instances", []) or [],
    }


def _get_counter(db: Database, period: str) -> UsageCounter:
    counter = UsageCounter.get_or_none(
        (UsageCounter.database == db) & (UsageCounter.period == period)
    )
    if counter is None:
        try:
            counter = UsageCounter.create(database=db, period=period)
        except Exception:
            counter = UsageCounter.get(
                (UsageCounter.database == db) & (UsageCounter.period == period)
            )
    return counter


def sync_usage(db: Database) -> UsageCounter:
    """Reconcile one counter from Turso's authoritative usage endpoint."""
    data = get_platform_client().get_db_usage(db.turso_name)
    total = data.get("total", {}) or {}
    counter = _get_counter(db, current_period())
    counter.rows_read = total.get("rows_read", 0)
    counter.rows_written = total.get("rows_written", 0)
    counter.storage_bytes = total.get("storage_bytes", 0)
    counter.last_synced_at = utcnow()
    counter.save()
    return counter


def sync_all_usage() -> int:
    """Periodic reconciliation for every live database. Returns count synced."""
    synced = 0
    for db in Database.select().where(Database.status.in_(("active", "suspended"))):
        try:
            sync_usage(db)
            synced += 1
        except AppError as exc:
            logger.warning("usage sync failed for %s: %s", db.id, exc)
    return synced


async def usage_sync_loop(interval: int | None = None) -> None:
    """Background task started from the app lifespan."""
    interval = interval or config.USAGE_SYNC_INTERVAL_SECONDS
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(sync_all_usage)
        except Exception as exc:  # never kill the loop
            logger.warning("usage sync loop error: %s", exc)


def record_usage(db: Database, rows_read: int = 0, rows_written: int = 0) -> None:
    """Hot-path metering: atomic increments on the current period counter."""
    period = current_period()
    _get_counter(db, period)
    UsageCounter.update(
        rows_read=UsageCounter.rows_read + max(rows_read, 0),
        rows_written=UsageCounter.rows_written + max(rows_written, 0),
        queries=UsageCounter.queries + 1,
    ).where((UsageCounter.database == db) & (UsageCounter.period == period)).execute()


def get_counter(db: Database, period: str | None = None) -> UsageCounter | None:
    period = period or current_period()
    return UsageCounter.get_or_none(
        (UsageCounter.database == db) & (UsageCounter.period == period)
    )


def check_quota(db: Database) -> None:
    """Raise QuotaExceeded if the current month's usage is at the plan limit."""
    owner = db.owner
    plan = config.plan_limits(owner.plan or config.DEFAULT_PLAN)
    counter = get_counter(db)
    used_read = counter.rows_read if counter else 0
    used_written = counter.rows_written if counter else 0
    used_storage = counter.storage_bytes if counter else 0

    if used_read >= plan.reads_per_month:
        raise QuotaExceeded(
            "Monthly read quota exceeded for your plan.",
            details={"resource": "rows_read", "used": used_read, "limit": plan.reads_per_month},
        )
    if used_written >= plan.writes_per_month:
        raise QuotaExceeded(
            "Monthly write quota exceeded for your plan.",
            details={"resource": "rows_written", "used": used_written,
                     "limit": plan.writes_per_month},
        )
    if used_storage >= plan.storage_bytes:
        raise QuotaExceeded(
            "Storage quota exceeded for your plan.",
            details={"resource": "storage_bytes", "used": used_storage,
                     "limit": plan.storage_bytes},
        )


def get_stats(user: User, db_id: str) -> dict:
    db = _owned_database(user, db_id)
    try:
        top_queries = get_platform_client().get_db_stats(db.turso_name)
    except TursoError as exc:
        raise UpstreamError("Could not fetch stats right now") from exc
    return {"database_id": db.id, "top_queries": top_queries or []}


# --- admin / ops -----------------------------------------------------------


def org_overview(user: User) -> dict:
    _require_admin(user)
    counts: dict[str, int] = {}
    for status in ("creating", "active", "suspended", "deleting", "failed", "deleted"):
        counts[status] = Database.select().where(Database.status == status).count()

    period = current_period()
    totals = {"rows_read": 0, "rows_written": 0, "queries": 0, "storage_bytes": 0}
    per_db = []
    for counter in UsageCounter.select().where(UsageCounter.period == period):
        db = counter.database
        if db.deleted_at is not None:
            continue
        totals["rows_read"] += counter.rows_read
        totals["rows_written"] += counter.rows_written
        totals["queries"] += counter.queries
        totals["storage_bytes"] += counter.storage_bytes
        per_db.append(
            {
                "database_id": db.id,
                "alias": db.alias,
                "owner_id": db.owner_id,
                "status": db.status,
                "rows_read": counter.rows_read,
                "rows_written": counter.rows_written,
                "queries": counter.queries,
                "storage_bytes": counter.storage_bytes,
            }
        )
    per_db.sort(key=lambda item: item["rows_read"], reverse=True)
    return {
        "period": period,
        "capacity": {
            "max_databases": config.TURSO_MAX_DATABASES,
            "used_databases": sum(counts[s] for s in COUNTED_STATUSES),
            "by_status": counts,
        },
        "totals": totals,
        "databases": per_db,
    }


def reconcile_with_turso(user: User) -> dict:
    """Report orphans (in Turso, not ours) and ghosts (ours, missing in Turso).

    Report only - never auto-deletes anything.
    """
    _require_admin(user)
    platform = get_platform_client()
    try:
        upstream = {item.get("Name") for item in platform.list_databases()}
    except TursoError as exc:
        raise UpstreamError("Could not list databases on the upstream") from exc

    ours = {
        db.turso_name: db
        for db in Database.select().where(Database.deleted_at.is_null())
    }
    orphans = sorted(name for name in upstream - set(ours) if name)
    ghosts = sorted(
        (
            {"database_id": db.id, "alias": db.alias, "status": db.status}
            for name, db in ours.items() if name not in upstream
        ),
        key=lambda item: item["alias"],
    )
    return {
        "turso_count": len(upstream),
        "our_count": len(ours),
        "orphans": orphans,
        "ghosts": ghosts,
    }


def health(user: User) -> dict:
    """Turso reachability, group state (unarchive if archived), capacity."""
    _require_admin(user)
    platform = get_platform_client()
    report: dict = {"ok": True, "checked_at": utcnow().isoformat()}
    try:
        locations = platform.list_locations()
        report["locations"] = len(locations)
        group = platform.ensure_group()
        report["group"] = group.get("name", config.TURSO_GROUP)
    except AppError as exc:
        report["ok"] = False
        report["error"] = str(exc)
        return report

    report["capacity"] = {
        "max_databases": config.TURSO_MAX_DATABASES,
        "used_databases": _capacity_used(),
    }
    return report
