"""Low-level connection to Turso: Platform API + SQL-over-HTTP (Hrana v2).

Everything that talks to Turso goes through this module. Business rules live
in ``turso.service``.
"""

from __future__ import annotations

import base64
import datetime as dt
import threading
import time

import httpx

from turso import config
from turso.errors import (
    TursoAuthError,
    TursoConflict,
    TursoNotFound,
    TursoPaymentRequired,
    TursoRateLimited,
    TursoUpstreamError,
)


def _iso(value: str | dt.datetime | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    if value.tzinfo is not None:
        value = value.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return value.isoformat() + "Z"


def _group_is_archived(group: dict) -> bool:
    """Best-effort archived detection (the spec does not expose a flag)."""
    if not isinstance(group, dict):
        return False
    for key in ("archived", "Archived", "is_archived"):
        if group.get(key):
            return True
    return str(group.get("status", "")).lower() == "archived"


class TursoPlatformClient:
    """Thin synchronous wrapper around the Turso Platform API.

    Retries with backoff only on idempotent calls (GET/DELETE) for network
    errors and 5xx. POSTs (create, token mint) are never auto-retried.
    """

    def __init__(
        self,
        org: str | None = None,
        token: str | None = None,
        base_url: str | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 2,
        backoff_base: float = 0.2,
    ):
        self.org = org or config.TURSO_ORG
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._client = httpx.Client(
            base_url=(base_url or config.TURSO_API_BASE_URL).rstrip("/"),
            headers={
                "Authorization": f"Bearer {token or config.TURSO_PLATFORM_TOKEN}",
                "Accept": "application/json",
            },
            timeout=timeout or config.TURSO_HTTP_TIMEOUT,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    # --- plumbing ----------------------------------------------------------

    def _org_path(self, suffix: str) -> str:
        return f"/v1/organizations/{self.org}{suffix}"

    @staticmethod
    def _to_error(response: httpx.Response) -> Exception:
        message = ""
        try:
            data = response.json()
            if isinstance(data, dict):
                message = str(data.get("error") or data.get("message") or "")
        except ValueError:
            message = (response.text or "")[:300]
        message = message.strip() or "no error details"
        status = response.status_code
        if status == 400:
            return TursoUpstreamError(f"Turso rejected the request: {message}")
        if status in (401, 403):
            return TursoAuthError(message)
        if status == 402:
            return TursoPaymentRequired(message)
        if status == 404:
            return TursoNotFound(message)
        if status == 409:
            return TursoConflict(message)
        if status == 429:
            return TursoRateLimited(message)
        return TursoUpstreamError(f"Turso API returned {status}: {message}")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        idempotent: bool | None = None,
    ) -> dict:
        if idempotent is None:
            idempotent = method in ("GET", "DELETE", "HEAD")
        attempts = 1 + (self._max_retries if idempotent else 0)
        for attempt in range(attempts):
            try:
                response = self._client.request(method, path, params=params, json=json)
            except httpx.HTTPError as exc:
                if attempt + 1 < attempts:
                    time.sleep(self._backoff_base * (2**attempt))
                    continue
                raise TursoUpstreamError(f"Could not reach Turso: {type(exc).__name__}") from exc
            if response.status_code < 300:
                if not response.content:
                    return {}
                try:
                    data = response.json()
                except ValueError:
                    return {}
                return data if isinstance(data, dict) else {"data": data}
            if response.status_code >= 500 and attempt + 1 < attempts:
                time.sleep(self._backoff_base * (2**attempt))
                continue
            raise self._to_error(response)
        raise TursoUpstreamError("Could not reach Turso")  # pragma: no cover

    # --- locations ---------------------------------------------------------

    def list_locations(self) -> dict[str, str]:
        """GET /v1/locations -> {"code": "Name"}."""
        data = self._request("GET", "/v1/locations")
        locations = data.get("locations", {})
        return locations if isinstance(locations, dict) else {}

    # --- groups ------------------------------------------------------------

    def get_group(self, name: str) -> dict:
        data = self._request("GET", self._org_path(f"/groups/{name}"))
        return data.get("group", data)

    def list_groups(self) -> list[dict]:
        data = self._request("GET", self._org_path("/groups"))
        return data.get("groups", []) or []

    def create_group(self, name: str, location: str) -> dict:
        data = self._request(
            "POST", self._org_path("/groups"), json={"name": name, "location": location}
        )
        return data.get("group", data)

    def unarchive_group(self, name: str) -> dict:
        data = self._request("POST", self._org_path(f"/groups/{name}/unarchive"))
        return data.get("group", data) if data else {}

    def ensure_group(self, name: str | None = None, location: str | None = None) -> dict:
        """Get-or-create the group (free plan allows one) and unarchive if needed."""
        name = name or config.TURSO_GROUP
        location = location or config.TURSO_LOCATION
        try:
            group = self.get_group(name)
        except TursoNotFound:
            try:
                return self.create_group(name, location)
            except TursoConflict:
                group = self.get_group(name)
        if _group_is_archived(group):
            group = self.unarchive_group(name)
        return group

    # --- databases ---------------------------------------------------------

    def create_database(
        self, name: str, group: str, size_limit: int | str | None = None, seed: dict | None = None
    ) -> dict:
        """POST .../databases. Response keys are capitalised."""
        body: dict = {"name": name, "group": group}
        if size_limit is not None:
            body["size_limit"] = str(size_limit)
        if seed is not None:
            body["seed"] = seed
        data = self._request("POST", self._org_path("/databases"), json=body)
        return data.get("database", data)

    def get_database(self, name: str) -> dict:
        data = self._request("GET", self._org_path(f"/databases/{name}"))
        return data.get("database", data)

    def list_databases(self) -> list[dict]:
        data = self._request("GET", self._org_path("/databases"))
        return data.get("databases", []) or []

    def delete_database(self, name: str) -> None:
        self._request("DELETE", self._org_path(f"/databases/{name}"))

    # --- database configuration -------------------------------------------

    def get_db_config(self, name: str) -> dict:
        data = self._request("GET", self._org_path(f"/databases/{name}/configuration"))
        return data.get("database", data) or data

    def update_db_config(self, name: str, **fields) -> dict:
        body = {key: value for key, value in fields.items() if value is not None}
        data = self._request(
            "PATCH", self._org_path(f"/databases/{name}/configuration"), json=body
        )
        return data.get("database", data) or data

    # --- usage / stats -----------------------------------------------------

    def get_db_usage(
        self,
        name: str,
        from_: str | dt.datetime | None = None,
        to: str | dt.datetime | None = None,
    ) -> dict:
        """GET .../usage -> {"database": {"uuid", "instances", "total"}}."""
        params: dict = {}
        if from_ is not None:
            params["from"] = _iso(from_)
        if to is not None:
            params["to"] = _iso(to)
        data = self._request(
            "GET", self._org_path(f"/databases/{name}/usage"), params=params or None
        )
        return data.get("database", data) or {}

    def get_db_stats(self, name: str) -> dict:
        """GET .../stats -> {"top_queries": [...]}."""
        data = self._request("GET", self._org_path(f"/databases/{name}/stats"))
        return data.get("top_queries", data) if isinstance(data, dict) else data

    def get_org_usage(self) -> dict:
        data = self._request("GET", self._org_path("/usage"))
        return data.get("organization", data) or data

    # --- auth tokens (Turso-side) -----------------------------------------

    def create_db_token(
        self, name: str, authorization: str = "full-access", expiration: str = "never"
    ) -> str:
        """POST .../auth/tokens -> {"jwt": "..."}."""
        data = self._request(
            "POST",
            self._org_path(f"/databases/{name}/auth/tokens"),
            params={"authorization": authorization, "expiration": expiration},
        )
        jwt = data.get("jwt")
        if not jwt:
            raise TursoUpstreamError("Turso returned no token")
        return jwt

    def rotate_db_tokens(self, name: str) -> None:
        """POST .../auth/rotate: invalidates ALL Turso tokens for the db."""
        self._request("POST", self._org_path(f"/databases/{name}/auth/rotate"))


# --- SQL over HTTP ---------------------------------------------------------


def to_hrana_value(value) -> dict:
    """Plain Python -> typed Hrana value (integers travel as strings)."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"type": "blob", "base64": base64.b64encode(bytes(value)).decode()}
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return {"type": "text", "value": value.isoformat()}
    return {"type": "text", "value": str(value)}


def from_hrana_value(value: dict):
    """Typed Hrana value -> plain Python."""
    kind = value.get("type")
    if kind == "null":
        return None
    if kind == "integer":
        return int(value["value"])
    if kind == "float":
        return float(value["value"])
    if kind == "text":
        return value.get("value")
    if kind == "blob":
        return base64.b64decode(value.get("base64", ""))
    return value


class TursoSqlClient:
    """Async client for a database's SQL-over-HTTP endpoints."""

    def __init__(self, timeout: float | None = None, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            timeout=timeout or config.TURSO_HTTP_TIMEOUT, transport=transport
        )

    async def close(self) -> None:
        await self._client.aclose()

    @staticmethod
    def base_url(hostname: str) -> str:
        hostname = (hostname or "").strip()
        if hostname.startswith(("http://", "https://")):
            return hostname.rstrip("/")
        return f"https://{hostname}"

    async def pipeline(self, hostname: str, jwt: str, body: dict) -> httpx.Response:
        return await self._client.post(
            f"{self.base_url(hostname)}/v2/pipeline",
            json=body,
            headers={"Authorization": f"Bearer {jwt}"},
        )

    async def health(self, hostname: str, jwt: str) -> int:
        response = await self._client.get(
            f"{self.base_url(hostname)}/health",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        return response.status_code

    async def dump(self, hostname: str, jwt: str) -> str:
        response = await self._client.get(
            f"{self.base_url(hostname)}/dump",
            headers={"Authorization": f"Bearer {jwt}"},
        )
        response.raise_for_status()
        return response.text


# --- shared client registry ------------------------------------------------

_lock = threading.Lock()
_platform: TursoPlatformClient | None = None
_sql: TursoSqlClient | None = None


def get_platform_client() -> TursoPlatformClient:
    global _platform
    with _lock:
        if _platform is None:
            _platform = TursoPlatformClient()
        return _platform


def get_sql_client() -> TursoSqlClient:
    global _sql
    with _lock:
        if _sql is None:
            _sql = TursoSqlClient()
        return _sql


def set_clients(*, platform: TursoPlatformClient | None = None,
                sql: TursoSqlClient | None = None) -> None:
    """Install pre-built clients (app lifespan, tests)."""
    global _platform, _sql
    with _lock:
        if platform is not None:
            _platform = platform
        if sql is not None:
            _sql = sql


async def reset_clients() -> None:
    """Close and clear both shared clients."""
    global _platform, _sql
    with _lock:
        platform, _platform = _platform, None
        sql, _sql = _sql, None
    if platform is not None:
        platform.close()
    if sql is not None:
        await sql.close()
