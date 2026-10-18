"""The gateway: our URL + our token in, Turso out. Customers never see Turso.

- ``/v2/pipeline`` is a transparent Hrana-over-HTTP pass-through (with token
  swap, metering and hostname scrubbing).
- ``/query`` and ``/batch`` are friendlier JSON endpoints over the same auth.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import asynccontextmanager

from fastapi import Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError

from turso import config, service
from turso.errors import (
    BodyTooLarge,
    ForbiddenError,
    QuotaExceeded,
    SqlError,
    TokenInvalid,
    TooManyRequests,
    UpstreamError,
    ValidationError,
)
from turso.models import Database
from turso.schemas import BatchRequest, QueryRequest
from turso.turso import from_hrana_value, get_sql_client, to_hrana_value

logger = logging.getLogger("turso.proxy")

CONCURRENCY_ACQUIRE_TIMEOUT = 10.0
_TURSO_HOST_RE = re.compile(r"\b[a-z0-9-]+-[a-z0-9-]+\.turso\.io\b")

_semaphores: dict[tuple[int, str], asyncio.Semaphore] = {}
_pending: set[asyncio.Task] = set()


def bearer_token(request: Request) -> str:
    """Strict Authorization header parsing: exactly ``Bearer <token>``."""
    header = request.headers.get("authorization", "")
    parts = header.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise TokenInvalid("Missing or invalid Authorization header")
    return parts[1].strip()


async def _read_json_body(request: Request) -> dict:
    body = await request.body()
    if len(body) > config.PROXY_MAX_BODY_BYTES:
        raise BodyTooLarge(f"Request body exceeds {config.PROXY_MAX_BODY_BYTES} bytes")
    try:
        data = json.loads(body)
    except ValueError:
        raise ValidationError("Request body must be valid JSON") from None
    if not isinstance(data, dict):
        raise ValidationError("Request body must be a JSON object")
    return data


# --- scrubbing -------------------------------------------------------------


def scrub_text(text: str, db: Database, jwt: str | None = None) -> str:
    """Remove Turso hostnames, internal db names and JWTs from text."""
    if not text:
        return text
    secrets = [db.turso_name, db.turso_hostname, f"https://{db.turso_hostname}"]
    if jwt:
        secrets.append(jwt)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    return _TURSO_HOST_RE.sub("[redacted]", text)


def scrub_response(data: dict, db: Database, jwt: str | None = None) -> dict:
    """Force base_url to null and scrub Turso hostnames/JWTs from errors."""
    data["base_url"] = None
    if isinstance(data.get("message"), str):
        data["message"] = scrub_text(data["message"], db, jwt)
    for result in data.get("results") or []:
        if not isinstance(result, dict):
            continue
        error = result.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            error["message"] = scrub_text(error["message"], db, jwt)
    return data


def extract_usage(data: dict) -> tuple[int, int]:
    """Sum rows_read/rows_written from every execute result in a pipeline."""
    rows_read = rows_written = 0
    for result in data.get("results") or []:
        if not isinstance(result, dict) or result.get("type") != "ok":
            continue
        response = result.get("response") or {}
        if response.get("type") != "execute":
            continue
        payload = response.get("result") or {}
        rows_read += int(payload.get("rows_read") or 0)
        rows_written += int(payload.get("rows_written") or 0)
    return rows_read, rows_written


def _looks_blocked(text: str) -> bool:
    upper = text.upper()
    return "BLOCKED" in upper or "DATABASE IS FULL" in upper


def _is_blocked(data: dict) -> bool:
    for result in data.get("results") or []:
        if isinstance(result, dict) and result.get("type") == "error":
            error = result.get("error") or {}
            if _looks_blocked(f"{error.get('code', '')} {error.get('message', '')}"):
                return True
    return False


# --- concurrency + metering ------------------------------------------------


def _semaphore(db_id: str) -> asyncio.Semaphore:
    """Per-database, per-event-loop concurrency limit."""
    key = (id(asyncio.get_running_loop()), db_id)
    sem = _semaphores.get(key)
    if sem is None:
        sem = asyncio.Semaphore(config.PROXY_MAX_CONCURRENT_PER_DB)
        _semaphores[key] = sem
    return sem


@asynccontextmanager
async def _concurrency(db_id: str):
    sem = _semaphore(db_id)
    acquired = False
    try:
        await asyncio.wait_for(sem.acquire(), timeout=CONCURRENCY_ACQUIRE_TIMEOUT)
        acquired = True
        yield
    except asyncio.TimeoutError as exc:
        raise TooManyRequests("Too many concurrent requests for this database") from exc
    finally:
        if acquired:
            sem.release()


def _spawn(coro) -> None:
    task = asyncio.ensure_future(coro)
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain_background() -> None:
    """Await fire-and-forget metering tasks (used by tests)."""
    while _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


def _meter(db: Database, token, rows_read: int, rows_written: int) -> None:
    _spawn(_meter_async(db, token, rows_read, rows_written))


async def _meter_async(db: Database, token, rows_read: int, rows_written: int) -> None:
    try:
        await asyncio.to_thread(service.record_usage, db, rows_read, rows_written)
        await asyncio.to_thread(service.touch_token, token)
    except Exception as exc:  # metering must never break the hot path
        logger.warning("metering failed for %s: %s", db.id, exc)


# --- shared forwarding -----------------------------------------------------


async def _prepare(db_id: str, request: Request, *, quota: bool = True):
    """Token auth (and optionally quota) shared by every gateway endpoint."""
    token, db = await asyncio.to_thread(service.resolve_token, bearer_token(request))
    if db.id != db_id:
        raise ForbiddenError("This token does not belong to this database")
    if quota:
        await asyncio.to_thread(service.check_quota, db)
    return token, db


async def forward_pipeline(db: Database, token, body: dict) -> dict:
    """Send a Hrana pipeline to Turso, retry once on 401, scrub and meter."""
    sql = get_sql_client()
    jwt = await asyncio.to_thread(service.get_turso_jwt, db, token.permission)
    async with _concurrency(db.id):
        response = await sql.pipeline(db.turso_hostname, jwt, body)
        if response.status_code == 401:
            jwt = await asyncio.to_thread(service.refresh_turso_jwt, db, token.permission)
            response = await sql.pipeline(db.turso_hostname, jwt, body)
        if response.status_code == 401:
            raise UpstreamError("Database authentication failed upstream")

    if response.status_code >= 400:
        text = scrub_text(response.text, db, jwt)
        if _looks_blocked(response.text):
            raise QuotaExceeded(
                "Database is blocked by a storage or usage limit. "
                "Check your plan quotas and try again.",
                details={"resource": "storage_bytes"},
            )
        raise UpstreamError(f"Database returned HTTP {response.status_code}: {text[:300]}")

    try:
        data = response.json()
    except ValueError:
        raise UpstreamError("Database returned an invalid response") from None
    if not isinstance(data, dict):
        raise UpstreamError("Database returned an invalid response")

    data = scrub_response(data, db, jwt)
    if _is_blocked(data):
        raise QuotaExceeded(
            "Database is blocked by a storage or usage limit. "
            "Check your plan quotas and try again.",
            details={"resource": "storage_bytes"},
        )

    rows_read, rows_written = extract_usage(data)
    _meter(db, token, rows_read, rows_written)
    return data


# --- gateway endpoints -----------------------------------------------------


async def pipeline(request: Request, db_id: str) -> JSONResponse:
    """POST /v1/db/{db_id}/v2/pipeline - transparent Hrana pass-through."""
    token, db = await _prepare(db_id, request)
    body = await _read_json_body(request)
    if not isinstance(body.get("requests"), list):
        raise ValidationError('Body must contain a "requests" array')
    data = await forward_pipeline(db, token, body)
    return JSONResponse(status_code=200, content=data)


async def health(request: Request, db_id: str) -> dict:
    """GET /v1/db/{db_id}/health - proxies Turso's /health."""
    token, db = await _prepare(db_id, request, quota=False)
    sql = get_sql_client()
    jwt = await asyncio.to_thread(service.get_turso_jwt, db, token.permission)
    async with _concurrency(db.id):
        status = await sql.health(db.turso_hostname, jwt)
        if status == 401:
            jwt = await asyncio.to_thread(service.refresh_turso_jwt, db, token.permission)
            status = await sql.health(db.turso_hostname, jwt)
    if status != 200:
        raise UpstreamError(f"Database health check failed (HTTP {status})")
    return {"status": "ok"}


def _execute_item(result: dict, db: Database, jwt: str | None) -> dict:
    if result.get("type") == "error":
        error = result.get("error") or {}
        message = scrub_text(str(error.get("message", "query failed")), db, jwt)
        return {"error": {"code": error.get("code"), "message": message}}
    response = result.get("response") or {}
    if response.get("type") != "execute":
        return {}
    payload = response.get("result") or {}
    last_id = payload.get("last_insert_rowid")
    if isinstance(last_id, str):
        last_id = int(last_id)
    return {
        "columns": [col.get("name") for col in payload.get("cols") or []],
        "rows": [
            [from_hrana_value(value) for value in row] for row in payload.get("rows") or []
        ],
        "affected_rows": payload.get("affected_row_count", 0),
        "last_insert_rowid": last_id,
        "duration_ms": payload.get("query_duration_ms"),
    }


def _sql_payload(body_bytes: bytes, model):
    if len(body_bytes) > config.PROXY_MAX_BODY_BYTES:
        raise BodyTooLarge(f"Request body exceeds {config.PROXY_MAX_BODY_BYTES} bytes")
    try:
        return model.model_validate_json(body_bytes)
    except PydanticValidationError as exc:
        raise ValidationError(
            "Invalid request body", details={"errors": json.loads(exc.json())[:5]}
        ) from None


async def query(request: Request, db_id: str) -> dict:
    """POST /v1/db/{db_id}/query - simple JSON query API."""
    token, db = await _prepare(db_id, request)
    payload: QueryRequest = _sql_payload(await request.body(), QueryRequest)

    stmt: dict = {"sql": payload.sql}
    if payload.args:
        stmt["args"] = [to_hrana_value(value) for value in payload.args]
    if payload.named_args:
        stmt["named_args"] = [
            {"name": name, "value": to_hrana_value(value)}
            for name, value in payload.named_args.items()
        ]
    body = {"requests": [{"type": "execute", "stmt": stmt}, {"type": "close"}]}

    data = await forward_pipeline(db, token, body)
    results = data.get("results") or []
    if not results:
        raise UpstreamError("Database returned no results")
    item = _execute_item(results[0], db, None)
    if "error" in item:
        raise SqlError(item["error"]["message"],
                       details={"code": item["error"].get("code")})
    return item


async def batch(request: Request, db_id: str) -> dict:
    """POST /v1/db/{db_id}/batch - run statements, optionally in a transaction."""
    token, db = await _prepare(db_id, request)
    payload: BatchRequest = _sql_payload(await request.body(), BatchRequest)
    if not payload.statements:
        raise ValidationError("statements must not be empty")

    stmts = []
    for statement in payload.statements:
        stmt: dict = {"sql": statement.sql}
        if statement.args:
            stmt["args"] = [to_hrana_value(value) for value in statement.args]
        if statement.named_args:
            stmt["named_args"] = [
                {"name": name, "value": to_hrana_value(value)}
                for name, value in statement.named_args.items()
            ]
        stmts.append({"type": "execute", "stmt": stmt})

    if not payload.transaction:
        body = {"requests": stmts + [{"type": "close"}]}
        data = await forward_pipeline(db, token, body)
        results = data.get("results") or []
        items = [_execute_item(result, db, None) for result in results[: len(stmts)]]
        return {"transaction": False, "committed": True, "results": items}

    # Two-phase transaction: BEGIN + statements, then COMMIT or ROLLBACK on the
    # same connection (baton), so a failed statement never commits partial work.
    begin = {"type": "execute", "stmt": {"sql": "BEGIN"}}
    phase1 = await forward_pipeline(db, token, {"requests": [begin] + stmts})
    results = phase1.get("results") or []
    baton = phase1.get("baton")
    items = [_execute_item(result, db, None) for result in results[1: 1 + len(stmts)]]
    failed = any("error" in item for item in items) or (
        results and results[0].get("type") == "error"
    )

    action = "ROLLBACK" if failed else "COMMIT"
    phase2 = await forward_pipeline(
        db, token, {"baton": baton, "requests": [
            {"type": "execute", "stmt": {"sql": action}}, {"type": "close"},
        ]}
    )
    committed = not failed
    second_results = phase2.get("results") or []
    if second_results and second_results[0].get("type") == "error":
        committed = False
    return {"transaction": True, "committed": committed, "results": items}


async def dump_database(db: Database) -> str:
    """Stream-friendly SQL dump of a database (customer export feature)."""
    sql = get_sql_client()
    jwt = await asyncio.to_thread(service.get_turso_jwt, db, "full-access")
    return await sql.dump(db.turso_hostname, jwt)
