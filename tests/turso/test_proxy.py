"""Gateway tests: token swap, scrubbing, read-only JWT, quotas, metering."""

import json

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from turso import config, proxy, service
from turso.errors import register_exception_handlers
from turso.models import ApiToken
from turso.router import gateway_router

LARGE_SQL = "SELECT " + "x" * 20_000


@pytest.fixture
def app():
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(gateway_router)
    return app


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def auth_header(raw: str) -> dict:
    return {"Authorization": f"Bearer {raw}"}


def pipeline_body(sql="SELECT 1"):
    return {"requests": [{"type": "execute", "stmt": {"sql": sql}}, {"type": "close"}]}


async def test_pipeline_swaps_token_and_scrubs_base_url(client, database, api_token, sql, platform):
    _token, raw = api_token
    response = await client.post(f"/v1/db/{database.id}/v2/pipeline",
                                 json=pipeline_body(), headers=auth_header(raw))
    assert response.status_code == 200
    data = response.json()
    # base_url must never reveal the Turso host
    assert data["base_url"] is None
    assert "turso.io" not in response.text
    # upstream got a Turso JWT, not our token
    sent = sql.requests[-1]
    assert sent["authorization"].startswith("Bearer turso-jwt-")
    assert sent["authorization"] != f"Bearer {raw}"
    assert sent["url"].endswith("/v2/pipeline")
    # ... and the platform call minted a full-access JWT
    assert platform.token_mints[-1]["authorization"] == "full-access"


async def test_read_only_token_uses_read_only_jwt(client, database, sql, platform):
    _token, raw = service.create_token(database.owner, database.id, "ro", "read-only")
    response = await client.post(f"/v1/db/{database.id}/v2/pipeline",
                                 json=pipeline_body(), headers=auth_header(raw))
    assert response.status_code == 200
    assert platform.token_mints[-1]["authorization"] == "read-only"
    assert "read-only" in sql.requests[-1]["authorization"]


async def test_invalid_token_returns_our_error_format(client, database):
    response = await client.post(f"/v1/db/{database.id}/v2/pipeline",
                                 json=pipeline_body(), headers=auth_header("dbc_wrong"))
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "invalid_token"
    assert "turso" not in json.dumps(body).lower()


async def test_missing_auth_header(client, database):
    response = await client.post(f"/v1/db/{database.id}/v2/pipeline", json=pipeline_body())
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_token"


async def test_token_for_other_database_is_rejected(client, database, user, plan_allows_many):
    other, _, _ = service.create_database(user, "otherdb")
    _token, raw = service.create_token(database.owner, database.id, "t")
    response = await client.post(f"/v1/db/{other.id}/v2/pipeline",
                                 json=pipeline_body(), headers=auth_header(raw))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_body_too_large(client, database, api_token, monkeypatch):
    _token, raw = api_token
    monkeypatch.setattr(config, "PROXY_MAX_BODY_BYTES", 64)
    response = await client.post(f"/v1/db/{database.id}/v2/pipeline",
                                 json=pipeline_body(), headers=auth_header(raw))
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "body_too_large"


async def test_invalid_json_body(client, database, api_token):
    _token, raw = api_token
    response = await client.post(f"/v1/db/{database.id}/v2/pipeline",
                                 content=b"not json", headers=auth_header(raw))
    assert response.status_code == 400


async def test_quota_exceeded_blocks_pipeline(client, database, api_token):
    _token, raw = api_token
    limits = config.plan_limits("free")
    service.record_usage(database, limits.reads_per_month, 0)
    response = await client.post(f"/v1/db/{database.id}/v2/pipeline",
                                 json=pipeline_body(), headers=auth_header(raw))
    assert response.status_code == 429
    body = response.json()
    assert body["error"]["code"] == "quota_exceeded"
    assert body["error"]["details"]["resource"] == "rows_read"


async def test_metering_records_rows_read_and_written(client, database, api_token):
    _token, raw = api_token
    response = await client.post(f"/v1/db/{database.id}/v2/pipeline",
                                 json=pipeline_body(), headers=auth_header(raw))
    assert response.status_code == 200
    await proxy.drain_background()

    counter = service.get_counter(database)
    assert counter.rows_read == 7
    assert counter.rows_written == 3
    assert counter.queries == 1
    token = ApiToken.get(ApiToken.id == _token.id)
    assert token.last_used_at is not None


async def test_upstream_401_re_mints_once_and_retries(client, database, api_token, sql, platform):
    _token, raw = api_token
    calls = {"n": 0}

    def responder(request, body):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(401, json={"error": "jwt expired"})
        return None

    sql.responder = responder
    response = await client.post(f"/v1/db/{database.id}/v2/pipeline",
                                 json=pipeline_body(), headers=auth_header(raw))
    assert response.status_code == 200
    assert calls["n"] == 2
    assert len(platform.token_mints) == 2  # initial mint + refresh after 401


async def test_upstream_401_twice_returns_502(client, database, api_token, sql):
    _token, raw = api_token
    sql.responder = lambda request, body: httpx.Response(401, json={"error": "nope"})
    response = await client.post(f"/v1/db/{database.id}/v2/pipeline",
                                 json=pipeline_body(), headers=auth_header(raw))
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_error"


async def test_error_messages_scrub_turso_hostname(client, database, api_token, sql):
    _token, raw = api_token
    hostname = database.turso_hostname
    sql.responder = lambda request, body: httpx.Response(200, json={
        "baton": None, "base_url": f"https://{hostname}",
        "results": [{"type": "error", "error": {
            "message": f"connection to {hostname} refused by {database.turso_name}",
            "code": "SERVER"}}],
    })
    response = await client.post(f"/v1/db/{database.id}/v2/pipeline",
                                 json=pipeline_body(), headers=auth_header(raw))
    assert response.status_code == 200
    text = response.text
    assert hostname not in text
    assert database.turso_name not in text
    assert "[redacted]" in text
    assert response.json()["base_url"] is None


async def test_blocked_result_maps_to_quota_error(client, database, api_token, sql):
    _token, raw = api_token
    sql.responder = lambda request, body: httpx.Response(200, json={
        "baton": None, "base_url": None,
        "results": [{"type": "error", "error": {
            "message": "database is blocked", "code": "BLOCKED"}}],
    })
    response = await client.post(f"/v1/db/{database.id}/v2/pipeline",
                                 json=pipeline_body(), headers=auth_header(raw))
    assert response.status_code == 429
    assert response.json()["error"]["code"] == "quota_exceeded"


async def test_upstream_http_error_maps_to_502(client, database, api_token, sql):
    _token, raw = api_token
    sql.responder = lambda request, body: httpx.Response(500, json={"error": "oops"})
    response = await client.post(f"/v1/db/{database.id}/v2/pipeline",
                                 json=pipeline_body(), headers=auth_header(raw))
    assert response.status_code == 502


async def test_health_endpoint(client, database, api_token):
    _token, raw = api_token
    response = await client.get(f"/v1/db/{database.id}/health", headers=auth_header(raw))
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --- simple JSON API --------------------------------------------------------


async def test_query_returns_plain_json_values(client, database, api_token, sql):
    _token, raw = api_token
    sql.responder = lambda request, body: httpx.Response(200, json={
        "baton": None, "base_url": None,
        "results": [
            {"type": "ok", "response": {"type": "execute", "result": {
                "cols": [{"name": "answer", "decltype": "INTEGER"},
                         {"name": "name", "decltype": "TEXT"}],
                "rows": [[{"type": "integer", "value": "42"},
                          {"type": "text", "value": "hi"}]],
                "affected_row_count": 1, "last_insert_rowid": "7",
                "rows_read": 2, "rows_written": 1, "query_duration_ms": 0.5}}},
            {"type": "ok", "response": {"type": "close"}},
        ]})

    response = await client.post(f"/v1/db/{database.id}/query",
                                 json={"sql": "SELECT 42, 'hi'", "args": [1, "a", None]},
                                 headers=auth_header(raw))
    assert response.status_code == 200
    payload = response.json()
    assert payload["columns"] == ["answer", "name"]
    assert payload["rows"] == [[42, "hi"]]
    assert payload["affected_rows"] == 1
    assert payload["last_insert_rowid"] == 7

    sent_stmt = sql.requests[-1]["body"]["requests"][0]["stmt"]
    assert sent_stmt["args"] == [
        {"type": "integer", "value": "1"},
        {"type": "text", "value": "a"},
        {"type": "null"},
    ]
    await proxy.drain_background()
    assert service.get_counter(database).rows_read == 2


async def test_query_named_args(client, database, api_token, sql):
    _token, raw = api_token
    response = await client.post(f"/v1/db/{database.id}/query",
                                 json={"sql": "SELECT :x", "named_args": {"x": 5}},
                                 headers=auth_header(raw))
    assert response.status_code == 200
    stmt = sql.requests[-1]["body"]["requests"][0]["stmt"]
    assert stmt["named_args"] == [{"name": "x", "value": {"type": "integer", "value": "5"}}]


async def test_query_sql_error_returns_400_with_scrub(client, database, api_token, sql):
    _token, raw = api_token
    sql.responder = lambda request, body: httpx.Response(200, json={
        "baton": None, "base_url": None,
        "results": [
            {"type": "error", "error": {
                "message": f'no such column: bogus on {database.turso_hostname}',
                "code": "SQL_ERROR"}},
            {"type": "ok", "response": {"type": "close"}},
        ]})
    response = await client.post(f"/v1/db/{database.id}/query",
                                 json={"sql": "SELECT bogus"}, headers=auth_header(raw))
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "sql_error"
    assert database.turso_hostname not in response.text


async def test_query_invalid_body(client, database, api_token):
    _token, raw = api_token
    response = await client.post(f"/v1/db/{database.id}/query",
                                 json={"args": []}, headers=auth_header(raw))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"


async def test_batch_without_transaction(client, database, api_token, sql):
    _token, raw = api_token
    response = await client.post(f"/v1/db/{database.id}/batch",
                                 json={"statements": [{"sql": "INSERT INTO t VALUES (1)"},
                                                      {"sql": "INSERT INTO t VALUES (2)"}]},
                                 headers=auth_header(raw))
    assert response.status_code == 200
    payload = response.json()
    assert payload["transaction"] is False
    assert len(payload["results"]) == 2
    requests = sql.requests[-1]["body"]["requests"]
    assert requests[-1] == {"type": "close"}


async def test_batch_transaction_commits_on_success(client, database, api_token, sql):
    _token, raw = api_token
    response = await client.post(f"/v1/db/{database.id}/batch",
                                 json={"statements": [{"sql": "INSERT INTO t VALUES (1)"},
                                                      {"sql": "INSERT INTO t VALUES (2)"}],
                                       "transaction": True},
                                 headers=auth_header(raw))
    assert response.status_code == 200
    payload = response.json()
    assert payload["transaction"] is True
    assert payload["committed"] is True
    assert len(payload["results"]) == 2

    phase1, phase2 = sql.requests[-2], sql.requests[-1]
    assert phase1["body"]["requests"][0]["stmt"]["sql"] == "BEGIN"
    assert "close" not in json.dumps(phase1["body"]["requests"])
    assert phase2["body"]["baton"] == "baton-1"
    assert phase2["body"]["requests"][0]["stmt"]["sql"] == "COMMIT"


async def test_batch_transaction_rolls_back_on_error(client, database, api_token, sql):
    _token, raw = api_token

    def responder(request, body):
        requests = body.get("requests", [])
        if requests and requests[0].get("stmt", {}).get("sql") == "BEGIN":
            results = [{"type": "ok", "response": {"type": "execute", "result": {
                "cols": [], "rows": [], "affected_row_count": 0, "last_insert_rowid": None}}}]
            results.append({"type": "ok", "response": {"type": "execute", "result": {
                "cols": [], "rows": [], "affected_row_count": 1, "last_insert_rowid": 1}}})
            results.append({"type": "error", "error": {"message": "constraint failed",
                                                       "code": "SQLITE_CONSTRAINT"}})
            return httpx.Response(200, json={"baton": "baton-1", "base_url": None,
                                             "results": results})
        return None

    sql.responder = responder
    response = await client.post(f"/v1/db/{database.id}/batch",
                                 json={"statements": [{"sql": "INSERT INTO t VALUES (1)"},
                                                      {"sql": "INSERT INTO t VALUES (2)"}],
                                       "transaction": True},
                                 headers=auth_header(raw))
    assert response.status_code == 200
    payload = response.json()
    assert payload["committed"] is False
    assert "error" in payload["results"][1]
    phase2 = sql.requests[-1]
    assert phase2["body"]["requests"][0]["stmt"]["sql"] == "ROLLBACK"
    # the failed statement's error message is scrubbed
    assert "constraint failed" in payload["results"][1]["error"]["message"]


async def test_concurrent_limit_returns_429(client, database, api_token, monkeypatch):
    _token, raw = api_token
    monkeypatch.setattr(config, "PROXY_MAX_CONCURRENT_PER_DB", 1)
    monkeypatch.setattr(proxy, "CONCURRENCY_ACQUIRE_TIMEOUT", 0.05)

    sem = proxy._semaphore(database.id)
    await sem.acquire()
    try:
        response = await client.post(f"/v1/db/{database.id}/v2/pipeline",
                                     json=pipeline_body(), headers=auth_header(raw))
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "too_many_requests"
    finally:
        sem.release()
