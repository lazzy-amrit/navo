"""Pydantic request/response models for our API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# --- databases -------------------------------------------------------------


class CreateDatabaseRequest(BaseModel):
    alias: str = Field(min_length=2, max_length=64,
                       description="Customer-visible name (our alias, not the Turso name)")
    region: str | None = None
    size_limit: int | None = Field(default=None, gt=0,
                                   description="Storage limit in bytes, capped by the plan")
    create_initial_token: bool = False


class RenameDatabaseRequest(BaseModel):
    alias: str = Field(min_length=2, max_length=64)


class DatabaseOut(BaseModel):
    id: str
    alias: str
    slug: str
    status: str
    plan: str
    region: str
    size_limit_bytes: int | None = None
    status_reason: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, db) -> "DatabaseOut":
        return cls(
            id=db.id, alias=db.alias, slug=db.slug, status=db.status, plan=db.plan,
            region=db.region, size_limit_bytes=db.size_limit_bytes,
            status_reason=db.status_reason, created_at=db.created_at, updated_at=db.updated_at,
        )


class ConnectionInfoOut(BaseModel):
    database_id: str
    url: str
    pipeline_path: str
    query_path: str
    health_path: str
    auth_header: str
    token_id: int | None = None


# --- tokens ----------------------------------------------------------------


class CreateTokenRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    permission: Literal["full-access", "read-only"] = "full-access"
    expires_in: int | None = Field(default=None, gt=0,
                                   description="Seconds until the token expires")


class TokenOut(BaseModel):
    id: int
    name: str
    prefix: str
    permission: str
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime

    @classmethod
    def from_model(cls, token) -> "TokenOut":
        return cls(
            id=token.id, name=token.name, prefix=token.prefix, permission=token.permission,
            expires_at=token.expires_at, revoked_at=token.revoked_at,
            last_used_at=token.last_used_at, created_at=token.created_at,
        )


class TokenCreatedOut(TokenOut):
    token: str = Field(description="Shown exactly once; store it now")

    @classmethod
    def from_model(cls, token, raw: str) -> "TokenCreatedOut":
        return cls(**TokenOut.from_model(token).model_dump(), token=raw)


class UsageOut(BaseModel):
    database_id: str
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    rows_read: int
    rows_written: int
    storage_bytes: int
    bytes_synced: int
    instances: list[dict[str, Any]] = []

    model_config = {"populate_by_name": True}


class CounterOut(BaseModel):
    database_id: str
    period: str
    rows_read: int
    rows_written: int
    queries: int
    storage_bytes: int
    last_synced_at: datetime | None = None


class StatsOut(BaseModel):
    database_id: str
    top_queries: list[Any] = []


class MessageOut(BaseModel):
    message: str


# --- gateway (simple JSON API) ---------------------------------------------


class QueryRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=100_000)
    args: list[Any] = []
    named_args: dict[str, Any] = {}


class Statement(BaseModel):
    sql: str = Field(min_length=1, max_length=100_000)
    args: list[Any] = []
    named_args: dict[str, Any] = {}


class BatchRequest(BaseModel):
    statements: list[Statement] = Field(min_length=1, max_length=100)
    transaction: bool = False


class QueryResult(BaseModel):
    columns: list[str] = []
    rows: list[list[Any]] = []
    affected_rows: int = 0
    last_insert_rowid: int | None = None
    duration_ms: float | None = None


# --- admin -----------------------------------------------------------------


class AdminOverviewOut(BaseModel):
    period: str
    capacity: dict[str, Any]
    totals: dict[str, int]
    databases: list[dict[str, Any]]


class ReconcileOut(BaseModel):
    turso_count: int
    our_count: int
    orphans: list[str]
    ghosts: list[dict[str, Any]]


class HealthOut(BaseModel):
    ok: bool
    checked_at: str
    locations: int | None = None
    group: str | None = None
    capacity: dict[str, Any] | None = None
    error: str | None = None
