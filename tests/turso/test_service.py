"""Service-layer tests: provisioning, aliases, tokens, quotas, admin ops."""

import pytest

from turso import config, service
from turso.errors import (
    AliasInvalid,
    AliasTaken,
    CapacityReached,
    DatabaseUnavailable,
    ForbiddenError,
    NotFoundError,
    PlanLimitReached,
    ProvisioningFailed,
    QuotaExceeded,
    TokenInvalid,
    UpstreamError,
    ValidationError,
)
from turso.models import ApiToken, AuditLog, Database

# --- create / list / rename / delete ---------------------------------------


def test_create_database_success(user, platform):
    db, info, initial = service.create_database(user, "my-app", region="aws-ap-south-1")

    assert db.status == "active"
    assert db.alias == "my-app"
    assert db.turso_hostname == f"{db.turso_name}-testorg.turso.io"
    assert db.turso_group == "default"
    assert db.plan == "free"
    assert db.size_limit_bytes == 60 * 1024 * 1024
    assert initial is None
    assert db.turso_name in platform.dbs
    assert "default" in platform.groups
    # size limit applied via config PATCH
    assert platform.configs[db.turso_name]["size_limit"] == str(60 * 1024 * 1024)
    # our public URL, never Turso's
    assert info["url"] == f"http://testserver/v1/db/{db.id}"
    assert "testorg" not in info["url"] and "turso.io" not in info["url"]
    assert AuditLog.select().where(AuditLog.action == "database.create").count() == 1


def test_create_database_with_initial_token(user, platform):
    db, _info, initial = service.create_database(user, "tokdb", create_initial_token=True)
    assert initial is not None
    token, raw = initial
    assert raw.startswith("dbc_")
    assert token.token_hash == service.crypto.hash_token(raw)


def test_create_database_rejects_bad_alias(user):
    with pytest.raises(AliasInvalid):
        service.create_database(user, "a")
    with pytest.raises(AliasInvalid):
        service.create_database(user, "-nope")
    with pytest.raises(AliasInvalid):
        service.create_database(user, "ends-")


def test_alias_is_normalised_to_lowercase(user):
    db, _info, _ = service.create_database(user, "My-App")
    assert db.alias == "my-app"


def test_create_database_plan_limit(user, platform):
    service.create_database(user, "one")
    with pytest.raises(PlanLimitReached):
        service.create_database(user, "two")
    assert Database.select().count() == 1


def test_pro_plan_also_allows_one_database(user, platform):
    user.plan = "pro"
    user.save()
    service.create_database(user, "one")
    with pytest.raises(PlanLimitReached):
        service.create_database(user, "two")


def test_global_capacity_reached(monkeypatch, user):
    monkeypatch.setattr(config, "TURSO_MAX_DATABASES", 0)
    with pytest.raises(CapacityReached):
        service.create_database(user, "any")
    db = Database.get_or_none(Database.alias == "any")
    assert db is None  # nothing recorded before the capacity check


def test_create_database_failure_marks_failed(user, platform):
    platform.fail_next("databases", 500)
    with pytest.raises(ProvisioningFailed):
        service.create_database(user, "broken")

    db = Database.get(Database.alias == "broken")
    assert db.status == "failed"
    assert db.status_reason
    assert AuditLog.select().where(AuditLog.action == "database.create_failed").count() == 1


def test_create_database_rejects_region_and_size_limit(user):
    with pytest.raises(ValidationError):
        service.create_database(user, "db1", region="mars-1")
    with pytest.raises(ValidationError):
        service.create_database(user, "db1", size_limit=999_999_999_999)


def test_list_and_get_database(user, other_user):
    db, _, _ = service.create_database(user, "mine")
    listed = service.list_databases(user)
    assert [d.id for d in listed] == [db.id]
    assert service.list_databases(other_user) == []
    assert service.get_database(user, db.id).id == db.id
    with pytest.raises(NotFoundError):
        service.get_database(other_user, db.id)


def test_rename_database(user, platform):
    db, _, _ = service.create_database(user, "oldname")
    calls_before = len(platform.requests)
    renamed = service.rename_database(user, db.id, "newname")
    assert renamed.alias == "newname"
    assert renamed.slug == "newname"
    assert len(platform.requests) == calls_before  # alias-only, no Turso call


def test_rename_conflict_and_validation(user, plan_allows_many):
    service.create_database(user, "first")
    db2, _, _ = service.create_database(user, "second")
    with pytest.raises(AliasTaken):
        service.rename_database(user, db2.id, "first")
    with pytest.raises(AliasInvalid):
        service.rename_database(user, db2.id, "Not Valid!")
    with pytest.raises(NotFoundError):
        service.rename_database(user, "db_missing", "whatever")


def test_delete_database_revokes_and_deletes(user, platform):
    db, _, _ = service.create_database(user, "gone")
    token, _raw = service.create_token(user, db.id, "t")

    service.delete_database(user, db.id)

    assert Database.get(Database.id == db.id).status == "deleted"
    assert Database.get(Database.id == db.id).deleted_at is not None
    assert ApiToken.get(ApiToken.id == token.id).revoked_at is not None
    assert db.turso_name not in platform.dbs
    with pytest.raises(NotFoundError):
        service.get_database(user, db.id)


def test_delete_database_is_retry_safe_after_upstream_failure(user, platform):
    db, _, _ = service.create_database(user, "flaky")
    platform.fail_next("DELETE", 400)
    with pytest.raises(UpstreamError):
        service.delete_database(user, db.id)
    assert Database.get(Database.id == db.id).status == "deleting"

    service.delete_database(user, db.id)  # retry succeeds
    assert Database.get(Database.id == db.id).status == "deleted"


def test_delete_database_treats_upstream_404_as_success(user, platform):
    db, _, _ = service.create_database(user, "ghost")
    platform.dbs.pop(db.turso_name, None)  # already gone upstream
    service.delete_database(user, db.id)
    assert Database.get(Database.id == db.id).status == "deleted"


# --- tokens ----------------------------------------------------------------


def test_create_token_returns_raw_once(user, platform):
    db, _, _ = service.create_database(user, "tokdb")
    token, raw = service.create_token(user, db.id, "ci", "read-only", expires_in=3600)

    assert raw.startswith("dbc_") and len(raw) == 47
    assert token.prefix == raw[:8]
    assert token.token_hash != raw
    assert token.permission == "read-only"
    assert token.expires_at is not None
    stored = ApiToken.get(ApiToken.id == token.id)
    assert stored.token_hash == service.crypto.hash_token(raw)


def test_create_token_validation(user):
    db, _, _ = service.create_database(user, "tokdb")
    with pytest.raises(ValidationError):
        service.create_token(user, db.id, "t", "admin")
    with pytest.raises(ValidationError):
        service.create_token(user, db.id, "  ")
    with pytest.raises(ValidationError):
        service.create_token(user, db.id, "t", expires_in=0)


def test_create_token_requires_active_database(user):
    db, _, _ = service.create_database(user, "tokdb")
    db.status = "suspended"
    db.save()
    with pytest.raises(DatabaseUnavailable):
        service.create_token(user, db.id, "t")


def test_resolve_token_ok_and_revoked(api_token, user, platform):
    token, raw = api_token
    resolved, db = service.resolve_token(raw)
    assert resolved.id == token.id
    assert db.id == token.database_id

    service.revoke_token(user, token.database_id, token.id)
    with pytest.raises(TokenInvalid):
        service.resolve_token(raw)


def test_resolve_token_invalid_and_expired(api_token, user):
    token, raw = api_token
    with pytest.raises(TokenInvalid):
        service.resolve_token("dbc_not-a-real-token")
    with pytest.raises(TokenInvalid):
        service.resolve_token("")

    token.expires_at = service.utcnow().replace(year=2000)
    token.save()
    service._invalidate_token_cache(token_hash=token.token_hash)
    with pytest.raises(TokenInvalid):
        service.resolve_token(raw)


def test_resolve_token_cache_is_invalidated_on_revoke(api_token, user):
    token, raw = api_token
    service.resolve_token(raw)  # warm cache
    service.revoke_token(user, token.database_id, token.id)
    with pytest.raises(TokenInvalid):
        service.resolve_token(raw)


def test_resolve_token_blocks_non_active_database(api_token, user):
    token, raw = api_token
    db = token.database
    db.status = "suspended"
    db.save()
    service._invalidate_token_cache()
    with pytest.raises(DatabaseUnavailable):
        service.resolve_token(raw)


def test_list_and_revoke_all_tokens(user, platform):
    db, _, _ = service.create_database(user, "tokdb")
    service.create_token(user, db.id, "a")
    service.create_token(user, db.id, "b")
    assert len(service.list_tokens(user, db.id)) == 2

    assert service.revoke_all_tokens(user, db.id) == 2
    assert all(t.revoked_at for t in service.list_tokens(user, db.id))
    assert service.revoke_all_tokens(user, db.id) == 0


def test_revoke_token_wrong_owner(user, other_user, platform):
    db, _, _ = service.create_database(user, "tokdb")
    token, _raw = service.create_token(user, db.id, "a")
    with pytest.raises(NotFoundError):
        service.revoke_token(other_user, db.id, token.id)


# --- Turso credentials ------------------------------------------------------


def test_get_turso_jwt_mints_once_and_caches(database, platform):
    jwt1 = service.get_turso_jwt(database, "full-access")
    jwt2 = service.get_turso_jwt(database, "full-access")
    assert jwt1 == jwt2
    assert len(platform.token_mints) == 1
    assert platform.token_mints[0]["authorization"] == "full-access"
    assert database.turso_token_full_enc.startswith("gAAAA")  # encrypted at rest


def test_read_only_uses_separate_jwt(database, platform):
    full = service.get_turso_jwt(database, "full-access")
    read_only = service.get_turso_jwt(database, "read-only")
    assert full != read_only
    assert [m["authorization"] for m in platform.token_mints] == ["full-access", "read-only"]


def test_refresh_turso_jwt_re_mints(database, platform):
    first = service.get_turso_jwt(database, "full-access")
    second = service.refresh_turso_jwt(database, "full-access")
    assert second != first
    assert len(platform.token_mints) == 2


def test_rotate_turso_credentials_admin_only(user, database, platform, admin):
    with pytest.raises(ForbiddenError):
        service.rotate_turso_credentials(user, database.id)

    service.get_turso_jwt(database, "full-access")
    service.rotate_turso_credentials(admin, database.id)

    assert any(path.endswith("/auth/rotate") for _m, path, _b in platform.requests)
    db = Database.get(Database.id == database.id)
    assert db.turso_token_full_enc is None
    assert db.turso_token_ro_enc is None


# --- usage / quota ----------------------------------------------------------


def test_get_usage_calls_turso(user, platform):
    db, _, _ = service.create_database(user, "usedb")
    platform.usage_totals.update({"rows_read": 123, "rows_written": 9, "storage_bytes": 4096})
    usage = service.get_usage(user, db.id)
    assert usage["rows_read"] == 123
    assert usage["rows_written"] == 9
    assert usage["storage_bytes"] == 4096


def test_get_usage_other_user_denied(user, other_user, platform):
    db, _, _ = service.create_database(user, "usedb")
    with pytest.raises(NotFoundError):
        service.get_usage(other_user, db.id)


def test_record_usage_increments_atomically(database, platform):
    service.record_usage(database, 10, 2)
    service.record_usage(database, 5, 1)
    counter = service.get_counter(database)
    assert counter.rows_read == 15
    assert counter.rows_written == 3
    assert counter.queries == 2


def test_sync_usage_reads_turso_totals(database, platform):
    platform.usage_totals.update({"rows_read": 500, "rows_written": 50, "storage_bytes": 8192})
    counter = service.sync_usage(database)
    assert counter.rows_read == 500
    assert counter.rows_written == 50
    assert counter.storage_bytes == 8192
    assert counter.last_synced_at is not None


def test_sync_all_usage(database, platform):
    platform.usage_totals.update({"rows_read": 7, "rows_written": 1})
    assert service.sync_all_usage() == 1


def test_check_quota_passes_for_fresh_database(database):
    service.check_quota(database)  # no exception


def test_check_quota_blocks_read_limit(database):
    limits = config.plan_limits("free")
    service.record_usage(database, limits.reads_per_month, 0)
    with pytest.raises(QuotaExceeded) as excinfo:
        service.check_quota(database)
    assert excinfo.value.details["resource"] == "rows_read"
    assert excinfo.value.status == 429


def test_check_quota_blocks_write_limit(database):
    limits = config.plan_limits("free")
    service.record_usage(database, 0, limits.writes_per_month)
    with pytest.raises(QuotaExceeded) as excinfo:
        service.check_quota(database)
    assert excinfo.value.details["resource"] == "rows_written"


def test_check_quota_blocks_storage(database):
    limits = config.plan_limits("free")
    service.record_usage(database, 0, 0)
    counter = service.get_counter(database)
    counter.storage_bytes = limits.storage_bytes
    counter.save()
    with pytest.raises(QuotaExceeded) as excinfo:
        service.check_quota(database)
    assert excinfo.value.details["resource"] == "storage_bytes"


def test_get_stats(user, platform):
    db, _, _ = service.create_database(user, "statdb")
    stats = service.get_stats(user, db.id)
    assert stats["top_queries"][0]["query"] == "SELECT 1"


# --- admin ------------------------------------------------------------------


def test_admin_overview(user, admin, platform):
    db, _, _ = service.create_database(user, "olddb")
    service.record_usage(db, 100, 10)
    overview = service.org_overview(admin)
    assert overview["capacity"]["max_databases"] == 100
    assert overview["capacity"]["by_status"]["active"] == 1
    assert overview["totals"]["rows_read"] == 100
    assert overview["databases"][0]["alias"] == "olddb"

    with pytest.raises(ForbiddenError):
        service.org_overview(user)


def test_suspend_and_resume(user, admin, platform):
    db, _, _ = service.create_database(user, "db1")
    with pytest.raises(ForbiddenError):
        service.suspend_database(user, db.id)

    service.suspend_database(admin, db.id)
    assert Database.get(Database.id == db.id).status == "suspended"
    assert platform.configs[db.turso_name]["block_reads"] is True
    assert platform.configs[db.turso_name]["block_writes"] is True

    service.resume_database(admin, db.id)
    assert Database.get(Database.id == db.id).status == "active"
    assert platform.configs[db.turso_name]["block_reads"] is False


def test_reconcile_reports_orphans_and_ghosts(user, admin, platform):
    db, _, _ = service.create_database(user, "mine")
    platform.add_db("d-orphan000001")          # in Turso, not ours
    platform.dbs.pop(db.turso_name)             # ours, missing in Turso

    report = service.reconcile_with_turso(admin)
    assert report["orphans"] == ["d-orphan000001"]
    assert report["ghosts"][0]["database_id"] == db.id
    assert report["our_count"] == 1

    with pytest.raises(ForbiddenError):
        service.reconcile_with_turso(user)


def test_health(admin, user, platform):
    report = service.health(admin)
    assert report["ok"] is True
    assert report["group"] == "default"
    assert report["capacity"]["used_databases"] == 0
    with pytest.raises(ForbiddenError):
        service.health(user)


def test_health_reports_turso_unreachable(admin, platform):
    platform.fail_next("/v1/locations", 500)
    platform.fail_next("/v1/locations", 500)
    platform.fail_next("/v1/locations", 500)
    report = service.health(admin)
    assert report["ok"] is False
    assert "error" in report


def test_connection_info_never_leaks_turso(user):
    db, info, _ = service.create_database(user, "noleak")
    assert "turso" not in info["url"]
    assert info["url"].endswith(f"/v1/db/{db.id}")
