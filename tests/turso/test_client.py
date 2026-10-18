"""Unit tests for the low-level Turso client (httpx.MockTransport, no network)."""

import datetime as dt

import httpx
import pytest

from turso.errors import (
    TursoAuthError,
    TursoConflict,
    TursoNotFound,
    TursoPaymentRequired,
    TursoRateLimited,
    TursoUpstreamError,
)
from turso.turso import (
    TursoPlatformClient,
    TursoSqlClient,
    from_hrana_value,
    to_hrana_value,
)

ORG = "testorg"


def make_client(handler, **kwargs) -> TursoPlatformClient:
    kwargs.setdefault("backoff_base", 0)
    return TursoPlatformClient(
        org=ORG, token="tok", base_url="https://api.turso.tech",
        transport=httpx.MockTransport(handler), **kwargs,
    )


def test_list_locations():
    def handler(request):
        assert request.url.path == "/v1/locations"
        assert request.headers["authorization"] == "Bearer tok"
        return httpx.Response(200, json={"locations": {"aws-ap-south-1": "Mumbai"}})

    assert make_client(handler).list_locations() == {"aws-ap-south-1": "Mumbai"}


def test_create_database_uses_capitalised_response_keys():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["body"] = request.read()
        return httpx.Response(200, json={
            "database": {"DbId": "uuid", "Hostname": "d-abc-testorg.turso.io", "Name": "d-abc"}})

    client = make_client(handler)
    out = client.create_database("d-abc", group="default", size_limit=1024)
    assert out == {"DbId": "uuid", "Hostname": "d-abc-testorg.turso.io", "Name": "d-abc"}
    assert seen["path"] == f"/v1/organizations/{ORG}/databases"
    assert b'"group":"default"' in seen["body"]
    assert b'"size_limit":"1024"' in seen["body"]


@pytest.mark.parametrize("status,error_type", [
    (400, TursoUpstreamError),
    (401, TursoAuthError),
    (402, TursoPaymentRequired),
    (403, TursoAuthError),
    (404, TursoNotFound),
    (409, TursoConflict),
    (429, TursoRateLimited),
    (500, TursoUpstreamError),
])
def test_error_mapping(status, error_type):
    def handler(request):
        return httpx.Response(status, json={"error": "boom"})

    with pytest.raises(error_type) as excinfo:
        make_client(handler).get_database("d-x")
    assert "boom" in str(excinfo.value)


def test_get_retries_5xx_with_backoff():
    attempts = []

    def handler(request):
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(500, json={"error": "flaky"})
        return httpx.Response(200, json={"database": {"Name": "d-x"}})

    out = make_client(handler).get_database("d-x")
    assert out["Name"] == "d-x"
    assert len(attempts) == 3


def test_post_is_never_retried():
    attempts = []

    def handler(request):
        attempts.append(1)
        return httpx.Response(500, json={"error": "nope"})

    with pytest.raises(TursoUpstreamError):
        make_client(handler).create_database("d-x", group="default")
    assert len(attempts) == 1


def test_delete_404_raises_turso_not_found():
    def handler(request):
        return httpx.Response(404, json={"error": "gone"})

    with pytest.raises(TursoNotFound):
        make_client(handler).delete_database("d-x")


def test_ensure_group_creates_when_missing():
    calls = []

    def handler(request):
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "GET":
            return httpx.Response(404, json={"error": "group not found"})
        return httpx.Response(200, json={"group": {"name": "default"}})

    group = make_client(handler).ensure_group("default", "aws-ap-south-1")
    assert group["name"] == "default"
    assert calls == [
        f"GET /v1/organizations/{ORG}/groups/default",
        f"POST /v1/organizations/{ORG}/groups",
    ]


def test_ensure_group_unarchives_archived_group():
    calls = []

    def handler(request):
        calls.append(f"{request.method} {request.url.path}")
        if request.method == "GET":
            return httpx.Response(200, json={"group": {"name": "default", "archived": True}})
        return httpx.Response(200, json={"group": {"name": "default"}})

    make_client(handler).ensure_group("default", "aws-ap-south-1")
    assert f"POST /v1/organizations/{ORG}/groups/default/unarchive" in calls


def test_group_conflict_falls_back_to_existing():
    gets = []

    def handler(request):
        if request.method == "GET":
            gets.append(1)
            if len(gets) == 1:
                return httpx.Response(404, json={"error": "missing"})
            return httpx.Response(200, json={"group": {"name": "default"}})
        if request.method == "POST" and request.url.path.endswith("/groups"):
            return httpx.Response(409, json={"error": "group already exists"})
        return httpx.Response(200, json={"group": {"name": "default"}})

    group = make_client(handler).ensure_group("default", "aws-ap-south-1")
    assert group["name"] == "default"
    assert len(gets) == 2


def test_usage_passes_from_and_to_params():
    seen = {}

    def handler(request):
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"database": {"total": {"rows_read": 1}}})

    out = make_client(handler).get_db_usage(
        "d-x", from_=dt.datetime(2026, 1, 1), to="2026-02-01T00:00:00Z")
    assert out["total"]["rows_read"] == 1
    assert seen["query"]["from"] == "2026-01-01T00:00:00Z"
    assert seen["query"]["to"] == "2026-02-01T00:00:00Z"


def test_create_db_token_sends_authorization_and_expiration():
    seen = {}

    def handler(request):
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"jwt": "abc.def.ghi"})

    jwt = make_client(handler).create_db_token("d-x", authorization="read-only", expiration="2w")
    assert jwt == "abc.def.ghi"
    assert seen["query"] == {"authorization": "read-only", "expiration": "2w"}


def test_rotate_db_tokens_calls_rotate_endpoint():
    seen = []

    def handler(request):
        seen.append(f"{request.method} {request.url.path}")
        return httpx.Response(200, json={})

    make_client(handler).rotate_db_tokens("d-x")
    assert seen == [f"POST /v1/organizations/{ORG}/databases/d-x/auth/rotate"]


def test_update_db_config_patches_fields():
    seen = {}

    def handler(request):
        seen["body"] = request.read()
        return httpx.Response(200, json={"size_limit": "62914560"})

    out = make_client(handler).update_db_config("d-x", size_limit="62914560", block_reads=True)
    assert out["size_limit"] == "62914560"
    assert b"block_reads" in seen["body"]


def test_get_db_stats_returns_top_queries():
    def handler(request):
        return httpx.Response(200, json={"top_queries": [{"query": "SELECT 1"}]})

    assert make_client(handler).get_db_stats("d-x") == [{"query": "SELECT 1"}]


# --- Hrana value conversion ------------------------------------------------


def test_hrana_integer_values_are_strings_on_the_wire():
    assert to_hrana_value(42) == {"type": "integer", "value": "42"}
    assert from_hrana_value({"type": "integer", "value": "42"}) == 42


def test_hrana_roundtrip_all_types():
    values = [None, 1, -9, 1.5, "hi", True, b"\x00\x01"]
    for value in values:
        encoded = to_hrana_value(value)
        decoded = from_hrana_value(encoded)
        if value is True:
            assert decoded == 1
        else:
            assert decoded == value
    assert to_hrana_value(True) == {"type": "integer", "value": "1"}
    assert to_hrana_value(b"ab") == {"type": "blob", "base64": "YWI="}


def test_sql_client_base_url():
    assert TursoSqlClient.base_url("d-x-org.turso.io") == "https://d-x-org.turso.io"
    assert TursoSqlClient.base_url("https://d-x-org.turso.io/") == "https://d-x-org.turso.io"
