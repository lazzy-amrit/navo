"""Fixtures: mocked Turso (Platform API + SQL-over-HTTP), users, databases."""

from __future__ import annotations

import json

import httpx
import pytest

from Database.models import User
from turso import proxy, service
from turso import turso as turso_mod
from turso.turso import TursoPlatformClient, TursoSqlClient

ORG = "testorg"
PLATFORM_TOKEN = "test-platform-token"


def _purge() -> None:
    from Database.models import Otp, RateLimit, RefreshToken
    from turso.models import ApiToken, AuditLog, Database, UsageCounter

    for model in (ApiToken, UsageCounter, AuditLog, Database, RefreshToken, RateLimit, Otp, User):
        model.delete().execute()
    service._invalidate_token_cache()


class FakeTursoPlatform:
    """In-memory stand-in for the Turso Platform API."""

    def __init__(self):
        self.groups: dict[str, dict] = {}
        self.dbs: dict[str, dict] = {}
        self.configs: dict[str, dict] = {}
        self.usage_totals = {"rows_read": 0, "rows_written": 0, "storage_bytes": 4096,
                             "bytes_synced": 0}
        self.top_queries = [{"query": "SELECT 1", "rows_read": 10, "rows_written": 0}]
        self.requests: list[tuple[str, str, str]] = []
        self.fail: list[tuple[str, int]] = []
        self.token_mints: list[dict] = []

    # -- helpers for tests -------------------------------------------------
    def fail_next(self, substring: str, status: int) -> None:
        self.fail.append((substring, status))

    def add_db(self, name: str) -> dict:
        row = {"Name": name, "DbId": f"uuid-{name}",
               "Hostname": f"{name}-{ORG}.turso.io"}
        self.dbs[name] = row
        self.configs.setdefault(name, {"size_limit": None, "block_reads": False,
                                       "block_writes": False, "delete_protection": False,
                                       "allowed_ips": []})
        return row

    # -- httpx handler -----------------------------------------------------
    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        raw_body = request.content.decode() if request.content else ""
        self.requests.append((request.method, path, raw_body))
        if request.headers.get("authorization") != f"Bearer {PLATFORM_TOKEN}":
            return httpx.Response(401, json={"error": "invalid platform token"})

        for index, (substring, status) in enumerate(self.fail):
            if substring in f"{request.method} {path}":
                self.fail.pop(index)
                return httpx.Response(status, json={"error": f"forced {status}"})

        body = json.loads(raw_body) if raw_body else {}
        parts = path.strip("/").split("/")

        if parts[:2] == ["v1", "locations"]:
            return httpx.Response(200, json={"locations": {"aws-ap-south-1": "AWS AP South"}})
        if parts[:2] != ["v1", "organizations"] or parts[2] != ORG:
            return httpx.Response(404, json={"error": "not found"})

        rest = parts[3:]
        return self._route(request, rest, body)

    def _route(self, request: httpx.Request, rest: list[str], body: dict) -> httpx.Response:
        method = request.method
        if rest[:1] == ["usage"] and method == "GET":
            return httpx.Response(200, json={"organization": dict(self.usage_totals)})

        if rest[:1] == ["groups"]:
            name = rest[1] if len(rest) > 1 else None
            action = rest[2] if len(rest) > 2 else None
            if len(rest) == 1:
                if method == "GET":
                    return httpx.Response(200, json={"groups": list(self.groups.values())})
                if method == "POST":
                    if body.get("name") in self.groups:
                        return httpx.Response(409, json={"error": "group already exists"})
                    group = {"name": body["name"], "primary": body.get("location", ""),
                             "locations": [body.get("location", "")]}
                    self.groups[body["name"]] = group
                    return httpx.Response(200, json={"group": group})
            if method == "GET" and action is None:
                if name not in self.groups:
                    return httpx.Response(404, json={"error": "group not found"})
                return httpx.Response(200, json={"group": self.groups[name]})
            if method == "POST" and action == "unarchive":
                if name not in self.groups:
                    return httpx.Response(404, json={"error": "group not found"})
                group = dict(self.groups[name])
                group.pop("archived", None)
                self.groups[name] = group
                return httpx.Response(200, json={"group": group})

        if rest[:1] == ["databases"]:
            name = rest[1] if len(rest) > 1 else None
            action = rest[2] if len(rest) > 2 else None
            if len(rest) == 1:
                if method == "GET":
                    return httpx.Response(200, json={"databases": list(self.dbs.values())})
                if method == "POST":
                    if body.get("name") in self.dbs:
                        return httpx.Response(409, json={"error": "database already exists"})
                    if body.get("group") not in self.groups:
                        return httpx.Response(400, json={"error": "group not found"})
                    return httpx.Response(200, json={"database": self.add_db(body["name"])})
            if method == "GET" and action is None:
                if name not in self.dbs:
                    return httpx.Response(404, json={"error": "database not found"})
                return httpx.Response(200, json={"database": self.dbs[name]})
            if method == "DELETE" and action is None:
                if name not in self.dbs:
                    return httpx.Response(404, json={"error": "database not found"})
                self.dbs.pop(name, None)
                self.configs.pop(name, None)
                return httpx.Response(200, json={"database": name})
            if action == "configuration":
                if name not in self.dbs:
                    return httpx.Response(404, json={"error": "database not found"})
                if method == "GET":
                    return httpx.Response(200, json=dict(self.configs[name]))
                if method == "PATCH":
                    self.configs[name].update(body)
                    return httpx.Response(200, json=dict(self.configs[name]))
            if action == "usage" and method == "GET":
                if name not in self.dbs:
                    return httpx.Response(404, json={"error": "database not found"})
                return httpx.Response(200, json={
                    "database": {"uuid": f"uuid-{name}", "instances": [],
                                 "total": dict(self.usage_totals)}})
            if action == "stats" and method == "GET":
                if name not in self.dbs:
                    return httpx.Response(404, json={"error": "database not found"})
                return httpx.Response(200, json={"top_queries": self.top_queries})
            if action == "auth" and len(rest) > 3:
                if name not in self.dbs:
                    return httpx.Response(404, json={"error": "database not found"})
                if rest[3] == "tokens" and method == "POST":
                    authorization = request.url.params.get("authorization", "full-access")
                    self.token_mints.append({"name": name, "authorization": authorization})
                    return httpx.Response(200, json={
                        "jwt": f"turso-jwt-{authorization}-{len(self.token_mints)}"})
                if rest[3] == "rotate" and method == "POST":
                    return httpx.Response(200, json={})

        return httpx.Response(404, json={"error": f"no route for {method} {' '.join(rest)}"})


def default_pipeline_response(body: dict | None) -> dict:
    requests = (body or {}).get("requests", [])
    results = []
    for item in requests:
        if item.get("type") == "execute":
            results.append({"type": "ok", "response": {"type": "execute", "result": {
                "cols": [], "rows": [], "affected_row_count": 0, "last_insert_rowid": None,
                "rows_read": 7, "rows_written": 3, "query_duration_ms": 1.5}}})
        else:
            results.append({"type": "ok", "response": {"type": "close"}})
    closed = any(item.get("type") == "close" for item in requests)
    return {"baton": None if closed else "baton-1",
            "base_url": "https://d-abc123def456-testorg.turso.io",
            "results": results}


class FakeSql:
    """In-memory stand-in for a database's SQL-over-HTTP endpoint."""

    def __init__(self):
        self.requests: list[dict] = []
        self.responder = None  # optional callable(request, body) -> Response | None

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append({
            "method": request.method,
            "url": str(request.url),
            "authorization": request.headers.get("authorization"),
            "body": body,
        })
        if self.responder is not None:
            response = self.responder(request, body)
            if response is not None:
                return response
        return httpx.Response(200, json=default_pipeline_response(body))


# --- fixtures --------------------------------------------------------------


@pytest.fixture(autouse=True)
def clean_state():
    _purge()
    yield
    _purge()
    proxy._pending.clear()


@pytest.fixture(autouse=True)
def platform():
    fake = FakeTursoPlatform()
    client = TursoPlatformClient(
        org=ORG, token=PLATFORM_TOKEN, transport=httpx.MockTransport(fake.handler),
        backoff_base=0,
    )
    turso_mod.set_clients(platform=client)
    yield fake


@pytest.fixture(autouse=True)
def sql():
    fake = FakeSql()
    client = TursoSqlClient(transport=httpx.MockTransport(fake.handler))
    turso_mod.set_clients(sql=client)
    yield fake


@pytest.fixture
def plan_allows_many(monkeypatch):
    """Temporarily lift the 1-database plan limit so a test can create several."""
    import dataclasses

    from turso import config

    free = config.PLANS["free"]
    monkeypatch.setitem(config.PLANS, "free", dataclasses.replace(free, databases=10))


@pytest.fixture
def user() -> User:
    return User.create(name="Test User", email="user@example.com")


@pytest.fixture
def other_user() -> User:
    return User.create(name="Other", email="other@example.com")


@pytest.fixture
def admin() -> User:
    return User.create(name="Admin", email="admin@example.com", is_admin=True)


@pytest.fixture
def database(user):
    db, _info, _token = service.create_database(user, "mydb")
    return db


@pytest.fixture
def api_token(database):
    token, raw = service.create_token(database.owner, database.id, "ci")
    return token, raw
