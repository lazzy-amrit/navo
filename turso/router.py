"""FastAPI routers: customer, gateway (our-token auth) and admin."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse

from Auth.services.jwt import bearer, get_current_user
from Auth.services.limiter import client_ip
from Database.models import User
from turso import proxy, service
from turso.errors import ForbiddenError
from turso.schemas import (
    ConnectionInfoOut,
    CounterOut,
    CreateDatabaseRequest,
    CreateTokenRequest,
    DatabaseOut,
    HealthOut,
    MessageOut,
    QueryResult,
    ReconcileOut,
    RenameDatabaseRequest,
    StatsOut,
    TokenCreatedOut,
    TokenOut,
    UsageOut,
)


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise ForbiddenError("Admin access required")
    return user


# --- customer ---------------------------------------------------------------

customer_router = APIRouter(prefix="/v1", tags=["databases"])


def _db_out(db) -> DatabaseOut:
    return DatabaseOut.from_model(db)


@customer_router.post("/databases", status_code=201)
def create_database(payload: CreateDatabaseRequest, request: Request,
                    user: User = Depends(get_current_user)):
    db, info, initial_token = service.create_database(
        user,
        payload.alias,
        region=payload.region,
        size_limit=payload.size_limit,
        create_initial_token=payload.create_initial_token,
        ip=client_ip(request),
    )
    created = None
    if initial_token is not None:
        token, raw = initial_token
        created = TokenCreatedOut.from_model(token, raw)
    return {
        "database": _db_out(db),
        "connection": ConnectionInfoOut(**info),
        "token": created,
    }


@customer_router.get("/databases", response_model=list[DatabaseOut])
def list_databases(user: User = Depends(get_current_user)):
    return [_db_out(db) for db in service.list_databases(user)]


@customer_router.get("/databases/{db_id}", response_model=DatabaseOut)
def get_database(db_id: str, user: User = Depends(get_current_user)):
    return _db_out(service.get_database(user, db_id))


@customer_router.patch("/databases/{db_id}", response_model=DatabaseOut)
def rename_database(db_id: str, payload: RenameDatabaseRequest,
                    user: User = Depends(get_current_user)):
    return _db_out(service.rename_database(user, db_id, payload.alias))


@customer_router.delete("/databases/{db_id}", status_code=204)
def delete_database(db_id: str, request: Request, user: User = Depends(get_current_user)):
    service.delete_database(user, db_id, ip=client_ip(request))
    return Response(status_code=204)


@customer_router.get("/databases/{db_id}/connection", response_model=ConnectionInfoOut)
def get_connection(db_id: str, user: User = Depends(get_current_user)):
    return ConnectionInfoOut(**service.get_connection_info(user, db_id))


@customer_router.get("/databases/{db_id}/usage", response_model=UsageOut)
def get_usage(
    db_id: str,
    user: User = Depends(get_current_user),
    from_: str | None = Query(None, alias="from"),
    to: str | None = Query(None),
):
    return service.get_usage(user, db_id, from_=from_, to=to)


@customer_router.get("/databases/{db_id}/counters", response_model=CounterOut)
def get_counter(db_id: str, user: User = Depends(get_current_user)):
    db = service.get_database(user, db_id)
    counter = service.get_counter(db)
    if counter is None:
        return CounterOut(database_id=db.id, period=service.current_period(),
                          rows_read=0, rows_written=0, queries=0, storage_bytes=0)
    return CounterOut(
        database_id=counter.database_id, period=counter.period,
        rows_read=counter.rows_read, rows_written=counter.rows_written,
        queries=counter.queries, storage_bytes=counter.storage_bytes,
        last_synced_at=counter.last_synced_at,
    )


@customer_router.get("/databases/{db_id}/stats", response_model=StatsOut)
def get_stats(db_id: str, user: User = Depends(get_current_user)):
    return service.get_stats(user, db_id)


@customer_router.post("/databases/{db_id}/tokens", response_model=TokenCreatedOut,
                      status_code=201)
def create_token(db_id: str, payload: CreateTokenRequest, request: Request,
                 user: User = Depends(get_current_user)):
    token, raw = service.create_token(
        user, db_id, payload.name, payload.permission,
        expires_in=payload.expires_in, ip=client_ip(request),
    )
    return TokenCreatedOut.from_model(token, raw)


@customer_router.get("/databases/{db_id}/tokens", response_model=list[TokenOut])
def list_tokens(db_id: str, user: User = Depends(get_current_user)):
    return [TokenOut.from_model(token) for token in service.list_tokens(user, db_id)]


@customer_router.delete("/databases/{db_id}/tokens/{token_id}", status_code=204)
def revoke_token(db_id: str, token_id: int, user: User = Depends(get_current_user)):
    service.revoke_token(user, db_id, token_id)
    return Response(status_code=204)


@customer_router.delete("/databases/{db_id}/tokens")
def revoke_all_tokens(db_id: str, user: User = Depends(get_current_user)):
    revoked = service.revoke_all_tokens(user, db_id)
    return {"message": f"Revoked {revoked} token(s)", "revoked": revoked}


@customer_router.get("/databases/{db_id}/export")
async def export_database(db_id: str, user: User = Depends(get_current_user)):
    db = await asyncio.to_thread(service.get_database, user, db_id)
    sql_text = await proxy.dump_database(db)
    filename = f"{db.alias}.sql"
    return StreamingResponse(
        iter([sql_text]),
        media_type="application/sql",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --- gateway (our tokens, not user sessions) --------------------------------

gateway_router = APIRouter(prefix="/v1/db", tags=["gateway"],
                           dependencies=[Depends(bearer)])


@gateway_router.post("/{db_id}/v2/pipeline", name="pipeline")
async def pipeline_endpoint(request: Request, db_id: str):
    return await proxy.pipeline(request, db_id)


@gateway_router.get("/{db_id}/health")
async def health_endpoint(request: Request, db_id: str):
    return await proxy.health(request, db_id)


@gateway_router.post("/{db_id}/query", response_model=QueryResult)
async def query_endpoint(request: Request, db_id: str):
    return await proxy.query(request, db_id)


@gateway_router.post("/{db_id}/batch")
async def batch_endpoint(request: Request, db_id: str):
    return await proxy.batch(request, db_id)


# --- admin ------------------------------------------------------------------

admin_router = APIRouter(prefix="/v1/admin", tags=["admin"])


@admin_router.get("/turso/health", response_model=HealthOut)
def admin_health(user: User = Depends(require_admin)):
    return service.health(user)


@admin_router.get("/turso/overview", response_model=None)
def admin_overview(user: User = Depends(require_admin)):
    return service.org_overview(user)


@admin_router.post("/turso/reconcile", response_model=ReconcileOut)
def admin_reconcile(user: User = Depends(require_admin)):
    return service.reconcile_with_turso(user)


@admin_router.post("/turso/usage-sync", response_model=MessageOut)
def admin_usage_sync(user: User = Depends(require_admin)):
    synced = service.sync_all_usage()
    return MessageOut(message=f"Synced usage for {synced} database(s)")


@admin_router.post("/databases/{db_id}/suspend", response_model=DatabaseOut)
def admin_suspend(db_id: str, user: User = Depends(require_admin)):
    return _db_out(service.suspend_database(user, db_id))


@admin_router.post("/databases/{db_id}/resume", response_model=DatabaseOut)
def admin_resume(db_id: str, user: User = Depends(require_admin)):
    return _db_out(service.resume_database(user, db_id))


@admin_router.post("/databases/{db_id}/rotate-turso-credentials", response_model=MessageOut)
def admin_rotate(db_id: str, user: User = Depends(require_admin)):
    service.rotate_turso_credentials(user, db_id)
    return MessageOut(
        message="Turso credentials rotated. Expect brief downtime until they are re-minted."
    )
