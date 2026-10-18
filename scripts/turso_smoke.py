"""Live smoke test against real Turso. Runs only when TURSO_LIVE=1.

Usage:
    TURSO_LIVE=1 .venv/bin/python scripts/turso_smoke.py

Creates a database, runs SQL through our proxy, checks usage, revokes the
token, verifies it stops working, then deletes the database.
"""

import asyncio
import os
import sys
import uuid

if os.environ.get("TURSO_LIVE") != "1":
    print("Refusing to run: set TURSO_LIVE=1 to hit the real Turso API.")
    sys.exit(0)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from Database.models import User  # noqa: E402
from turso import turso as turso_client  # noqa: E402
from turso.errors import register_exception_handlers  # noqa: E402
from turso.models import create_tables  # noqa: E402
from turso.router import customer_router, gateway_router  # noqa: E402

STEPS = []


def step(name: str, ok: bool, detail: str = "") -> None:
    STEPS.append((name, ok, detail))
    print(f"  [{'ok' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def build_app() -> FastAPI:
    app = FastAPI(title="InSQL smoke")
    register_exception_handlers(app)
    app.include_router(customer_router)
    app.include_router(gateway_router)
    return app


async def main() -> int:
    create_tables()
    suffix = uuid.uuid4().hex[:8]
    user = User.create(name="Smoke", email=f"smoke-{suffix}@example.com")

    app = build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://smoke",
                                 timeout=60) as client:
        alias = f"smoke-{suffix}"
        print(f"1. creating database {alias!r} (group + size limit on Turso)")
        response = await client.post("/v1/databases", json={"alias": alias})
        step("create database", response.status_code == 201, response.text[:300])
        if response.status_code != 201:
            return 1
        db_id = response.json()["database"]["id"]

        print("2. minting our API token")
        response = await client.post(f"/v1/databases/{db_id}/tokens",
                                     json={"name": "smoke"})
        step("create token", response.status_code == 201, response.text[:200])
        if response.status_code != 201:
            return 1
        raw_token = response.json()["token"]
        headers = {"Authorization": f"Bearer {raw_token}"}

        print("3. CREATE TABLE / INSERT / SELECT through our proxy")
        for sql in [
            "CREATE TABLE IF NOT EXISTS smoke (id INTEGER PRIMARY KEY, name TEXT)",
            "INSERT INTO smoke (name) VALUES ('navo')",
        ]:
            response = await client.post(f"/v1/db/{db_id}/query", json={"sql": sql},
                                         headers=headers)
            step(f"query: {sql[:40]}", response.status_code == 200, response.text[:300])
            if response.status_code != 200:
                await cleanup(client, user, db_id, raw_token)
                return 1

        response = await client.post(f"/v1/db/{db_id}/query",
                                     json={"sql": "SELECT name FROM smoke"},
                                     headers=headers)
        ok = response.status_code == 200 and response.json()["rows"] == [["navo"]]
        step("select returns the row", ok, response.text[:300])

        print("4. raw Hrana pipeline pass-through")
        response = await client.post(
            f"/v1/db/{db_id}/v2/pipeline",
            json={"requests": [{"type": "execute",
                                "stmt": {"sql": "SELECT COUNT(*) FROM smoke"}},
                               {"type": "close"}]},
            headers=headers,
        )
        body = response.json() if response.status_code == 200 else {}
        ok = response.status_code == 200 and body.get("base_url") is None
        step("pipeline + base_url scrubbed", ok, response.text[:300])

        print("5. usage endpoints")
        response = await client.get(f"/v1/databases/{db_id}/usage")
        step("get usage", response.status_code == 200, response.text[:300])
        response = await client.get(f"/v1/databases/{db_id}/counters")
        step("get counters", response.status_code == 200, response.text[:300])

        print("6. revoke token -> must stop working")
        tokens = await client.get(f"/v1/databases/{db_id}/tokens")
        token_id = tokens.json()[0]["id"]
        response = await client.delete(f"/v1/databases/{db_id}/tokens/{token_id}")
        step("revoke token", response.status_code == 204, response.text[:200])
        response = await client.post(f"/v1/db/{db_id}/query",
                                     json={"sql": "SELECT 1"}, headers=headers)
        step("revoked token rejected", response.status_code == 401, response.text[:200])

        await cleanup(client, user, db_id, None)

    print("\nSummary:")
    failed = [name for name, ok, _ in STEPS if not ok]
    for name, ok, detail in STEPS:
        print(f"  {'ok  ' if ok else 'FAIL'} {name}")
    return 1 if failed else 0


async def cleanup(client: httpx.AsyncClient, user: User, db_id: str, _raw) -> None:
    print("7. deleting the database")
    response = await client.delete(f"/v1/databases/{db_id}")
    step("delete database", response.status_code == 204, response.text[:300])
    user.delete_instance(recursive=True)
    await turso_client.reset_clients()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
