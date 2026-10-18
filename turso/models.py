"""Peewee models for our metadata database (not customer data)."""

from __future__ import annotations

import secrets

from peewee import (
    BigIntegerField,
    CharField,
    DateTimeField,
    ForeignKeyField,
    Model,
    TextField,
)

from Database import db, utcnow
from Database.models import User

DB_STATUSES = ("creating", "active", "suspended", "deleting", "deleted", "failed")
PERMISSIONS = ("full-access", "read-only")


class BaseModel(Model):
    class Meta:
        database = db


def new_database_id() -> str:
    return f"db_{secrets.token_urlsafe(12)}"


def new_turso_name() -> str:
    """Internal Turso name: ``d-`` + 12 lowercase chars (never shown to customers)."""
    return f"d-{secrets.token_hex(6)}"


class Database(BaseModel):
    id = CharField(primary_key=True, max_length=40)
    owner = ForeignKeyField(User, backref="databases", on_delete="CASCADE")
    alias = CharField(max_length=64, index=True)
    slug = CharField(max_length=64)
    turso_name = CharField(max_length=64, unique=True)
    turso_hostname = CharField(max_length=255, default="")
    turso_group = CharField(max_length=64, default="")
    region = CharField(max_length=32, default="")
    status = CharField(max_length=16, default="creating", index=True)
    plan = CharField(max_length=16, default="free")
    size_limit_bytes = BigIntegerField(null=True)
    status_reason = TextField(null=True)
    turso_token_full_enc = TextField(null=True)
    turso_token_ro_enc = TextField(null=True)
    created_at = DateTimeField(default=utcnow)
    updated_at = DateTimeField(default=utcnow)
    deleted_at = DateTimeField(null=True)

    class Meta:
        table_name = "database"

    def save(self, *args, **kwargs):
        self.updated_at = utcnow()
        return super().save(*args, **kwargs)


class ApiToken(BaseModel):
    """One of *our* tokens. The raw value is shown exactly once."""

    database = ForeignKeyField(Database, backref="tokens", on_delete="CASCADE")
    name = CharField(max_length=80)
    prefix = CharField(max_length=8, index=True)
    token_hash = CharField(max_length=64, unique=True)
    permission = CharField(max_length=16, default="full-access")
    expires_at = DateTimeField(null=True)
    revoked_at = DateTimeField(null=True)
    last_used_at = DateTimeField(null=True)
    created_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "api_token"

    @property
    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        return self.expires_at is None or self.expires_at > utcnow()


class UsageCounter(BaseModel):
    """Per-database, per-calendar-month metering (UTC)."""

    database = ForeignKeyField(Database, backref="usage_counters", on_delete="CASCADE")
    period = CharField(max_length=7)
    rows_read = BigIntegerField(default=0)
    rows_written = BigIntegerField(default=0)
    queries = BigIntegerField(default=0)
    storage_bytes = BigIntegerField(default=0)
    last_synced_at = DateTimeField(null=True)

    class Meta:
        table_name = "usage_counter"
        indexes = ((("database", "period"), True),)


class AuditLog(BaseModel):
    owner = ForeignKeyField(User, null=True, backref="audit_logs", on_delete="SET NULL")
    database = ForeignKeyField(Database, null=True, backref="audit_logs", on_delete="SET NULL")
    action = CharField(max_length=64, index=True)
    metadata = TextField(default="{}")
    ip = CharField(max_length=64, null=True)
    created_at = DateTimeField(default=utcnow, index=True)

    class Meta:
        table_name = "audit_log"


def create_tables():
    """Idempotent bootstrap for every table in the metadata database."""
    from Database.models import Otp, RateLimit, RefreshToken

    db.connect(reuse_if_open=True)
    db.create_tables(
        [User, RefreshToken, RateLimit, Otp, Database, ApiToken, UsageCounter, AuditLog],
        safe=True,
    )
    db.execute_sql(
        "CREATE UNIQUE INDEX IF NOT EXISTS database_owner_alias_active "
        "ON database (owner_id, alias) WHERE deleted_at IS NULL"
    )
    return db
