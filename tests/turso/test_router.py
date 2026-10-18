"""End-to-end API tests: customer routes, gateway, admin routes, /docs."""

import httpx
import pytest
from fastapi.testclient import TestClient

from Auth.services.jwt import create_access_token
from turso import service
from turso.models import Database


@pytest.fixture
def client():
    from main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def auth(user):
    return {"Authorization": f"Bearer {create_access_token(user.id)}"}


@pytest.fixture
def admin_auth(admin):
    return {"Authorization": f"Bearer {create_access_token(admin.id)}"}


def test_docs_loads(client):
    response = client.get("/docs")
    assert response.status_code == 200
    assert "InSQL" in response.text


def test_health_meta(client):
    assert client.get("/health").json() == {"status": "ok"}


# --- customer ---------------------------------------------------------------


def test_databases_require_auth(client):
    response = client.get("/v1/databases")
    assert response.status_code == 401


def test_create_list_get_rename_delete(client, user, auth, platform):
    response = client.post("/v1/databases", json={"alias": "my-app"}, headers=auth)
    assert response.status_code == 201, response.text
    payload = response.json()
    db = payload["database"]
    assert db["status"] == "active"
    assert db["alias"] == "my-app"
    assert payload["connection"]["url"] == f"http://testserver/v1/db/{db['id']}"
    assert payload["token"] is None
    db_id = db["id"]
    assert db_id in platform.dbs or any(v["Hostname"] for v in platform.dbs.values())

    listed = client.get("/v1/databases", headers=auth).json()
    assert [item["id"] for item in listed] == [db_id]

    fetched = client.get(f"/v1/databases/{db_id}", headers=auth)
    assert fetched.status_code == 200
    assert fetched.json()["alias"] == "my-app"

    renamed = client.patch(f"/v1/databases/{db_id}", json={"alias": "renamed"}, headers=auth)
    assert renamed.status_code == 200
    assert renamed.json()["alias"] == "renamed"

    conn = client.get(f"/v1/databases/{db_id}/connection", headers=auth).json()
    assert conn["url"] == f"http://testserver/v1/db/{db_id}"
    assert "turso" not in conn["url"].lower() and "turso.io" not in conn["url"]

    assert client.delete(f"/v1/databases/{db_id}", headers=auth).status_code == 204
    assert client.get(f"/v1/databases/{db_id}", headers=auth).status_code == 404
    assert Database.get(Database.id == db_id).status == "deleted"


def test_create_initial_token_in_response(client, user, auth):
    response = client.post("/v1/databases", json={"alias": "with-tok", "create_initial_token": True},
                           headers=auth)
    assert response.status_code == 201
    token = response.json()["token"]
    assert token["token"].startswith("dbc_")
    assert token["permission"] == "full-access"


def test_plan_limit_returns_our_error_format(client, user, auth):
    assert client.post("/v1/databases", json={"alias": "one"}, headers=auth).status_code == 201
    response = client.post("/v1/databases", json={"alias": "two"}, headers=auth)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "plan_limit_reached"


def test_alias_conflict(client, user, auth):
    client.post("/v1/databases", json={"alias": "dupe"}, headers=auth)
    response = client.post("/v1/databases", json={"alias": "dupe"}, headers=auth)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "alias_taken"


def test_tokens_lifecycle(client, user, auth):
    db_id = client.post("/v1/databases", json={"alias": "tokdb"}, headers=auth).json()[
        "database"]["id"]

    created = client.post(f"/v1/databases/{db_id}/tokens",
                          json={"name": "ci", "permission": "read-only"}, headers=auth)
    assert created.status_code == 201
    body = created.json()
    raw = body["token"]
    assert raw.startswith("dbc_")
    token_id = body["id"]

    listed = client.get(f"/v1/databases/{db_id}/tokens", headers=auth).json()
    assert len(listed) == 1
    assert "token" not in listed[0]
    assert listed[0]["prefix"] == raw[:8]
    assert listed[0]["permission"] == "read-only"

    # still valid until revoked
    assert service.resolve_token(raw)[1].id == db_id

    assert client.delete(f"/v1/databases/{db_id}/tokens/{token_id}", headers=auth).status_code == 204
    with pytest.raises(service.TokenInvalid):
        service.resolve_token(raw)

    # revoke-all
    client.post(f"/v1/databases/{db_id}/tokens", json={"name": "b"}, headers=auth)
    response = client.delete(f"/v1/databases/{db_id}/tokens", headers=auth)
    assert response.status_code == 200
    assert response.json()["revoked"] == 1


def test_usage_and_stats_and_counters(client, user, auth, platform):
    db_id = client.post("/v1/databases", json={"alias": "usedb"}, headers=auth).json()[
        "database"]["id"]
    platform.usage_totals.update({"rows_read": 42, "rows_written": 7, "storage_bytes": 4096})

    usage = client.get(f"/v1/databases/{db_id}/usage", headers=auth).json()
    assert usage["rows_read"] == 42
    assert usage["rows_written"] == 7

    stats = client.get(f"/v1/databases/{db_id}/stats", headers=auth).json()
    assert stats["database_id"] == db_id
    assert stats["top_queries"][0]["query"] == "SELECT 1"

    counters = client.get(f"/v1/databases/{db_id}/counters", headers=auth).json()
    assert counters["period"] == service.current_period()
    assert counters["rows_read"] == 0


def test_export_streams_sql_dump(client, user, auth, sql):
    db_id = client.post("/v1/databases", json={"alias": "dumpdb"}, headers=auth).json()[
        "database"]["id"]
    sql.responder = lambda request, body: (
        httpx.Response(200, text="CREATE TABLE t(x);\n") if request.url.path.endswith("/dump")
        else None
    )
    response = client.get(f"/v1/databases/{db_id}/export", headers=auth)
    assert response.status_code == 200
    assert response.text == "CREATE TABLE t(x);\n"
    assert "dumpdb.sql" in response.headers["content-disposition"]


def test_databases_are_isolated_between_users(client, user, auth, platform):
    from Database.models import User

    other = User.create(name="Other", email="other@example.com")
    other_auth = {"Authorization": f"Bearer {create_access_token(other.id)}"}
    db_id = client.post("/v1/databases", json={"alias": "mine"}, headers=auth).json()[
        "database"]["id"]

    assert client.get(f"/v1/databases/{db_id}", headers=other_auth).status_code == 404
    assert client.delete(f"/v1/databases/{db_id}", headers=other_auth).status_code == 404
    assert client.get("/v1/databases", headers=other_auth).json() == []


# --- gateway via the full app ----------------------------------------------


def test_pipeline_via_full_app(client, user, auth):
    db_id = client.post("/v1/databases", json={"alias": "gwdb"}, headers=auth).json()[
        "database"]["id"]
    _token, raw = service.create_token(user, db_id, "gw")

    response = client.post(f"/v1/db/{db_id}/v2/pipeline",
                           json={"requests": [{"type": "execute",
                                               "stmt": {"sql": "SELECT 1"}},
                                              {"type": "close"}]},
                           headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 200
    assert response.json()["base_url"] is None

    health = client.get(f"/v1/db/{db_id}/health", headers={"Authorization": f"Bearer {raw}"})
    assert health.status_code == 200


# --- admin ------------------------------------------------------------------


def test_admin_requires_flag(client, user, auth):
    for method, path in [
        ("get", "/v1/admin/turso/health"),
        ("get", "/v1/admin/turso/overview"),
        ("post", "/v1/admin/turso/reconcile"),
        ("post", "/v1/admin/turso/usage-sync"),
    ]:
        response = getattr(client, method)(path, headers=auth)
        assert response.status_code == 403, path
        assert response.json()["error"]["code"] == "forbidden"


def test_admin_endpoints(client, admin_auth, user, auth):
    db_id = client.post("/v1/databases", json={"alias": "admindb"}, headers=auth).json()[
        "database"]["id"]

    health = client.get("/v1/admin/turso/health", headers=admin_auth).json()
    assert health["ok"] is True
    assert health["group"] == "default"

    overview = client.get("/v1/admin/turso/overview", headers=admin_auth).json()
    assert overview["capacity"]["max_databases"] == 100

    reconcile = client.post("/v1/admin/turso/reconcile", headers=admin_auth).json()
    assert reconcile["orphans"] == []
    assert reconcile["ghosts"] == []

    sync = client.post("/v1/admin/turso/usage-sync", headers=admin_auth).json()
    assert "Synced" in sync["message"]

    suspended = client.post(f"/v1/admin/databases/{db_id}/suspend", headers=admin_auth)
    assert suspended.status_code == 200
    assert suspended.json()["status"] == "suspended"

    resumed = client.post(f"/v1/admin/databases/{db_id}/resume", headers=admin_auth)
    assert resumed.json()["status"] == "active"

    rotated = client.post(f"/v1/admin/databases/{db_id}/rotate-turso-credentials",
                          headers=admin_auth)
    assert rotated.status_code == 200
    assert "brief downtime" in rotated.json()["message"]


def test_admin_auth_required_at_all(client, admin_auth):
    assert client.get("/v1/admin/turso/health").status_code == 401
