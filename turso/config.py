"""Environment-driven settings and plan limits (source of truth: price.md)."""

import os
from dataclasses import dataclass

from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing required environment variable {name}. "
            f"Set it in your environment or .env (see turso/.env.example)."
        )
    return value


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer, got {raw!r}") from exc


TURSO_ORG = _require("TURSO_ORG")
TURSO_PLATFORM_TOKEN = _require("TURSO_PLATFORM_TOKEN")
TURSO_ENC_KEY = _require("TURSO_ENC_KEY")
TURSO_GROUP = os.getenv("TURSO_GROUP", "default")
TURSO_LOCATION = os.getenv("TURSO_LOCATION", "aws-ap-south-1")
TURSO_API_BASE_URL = os.getenv("TURSO_API_BASE_URL", "https://api.turso.tech")
TURSO_MAX_DATABASES = _int_env("TURSO_MAX_DATABASES", 100)
TURSO_HTTP_TIMEOUT = float(os.getenv("TURSO_HTTP_TIMEOUT", "15"))
PROXY_MAX_BODY_BYTES = _int_env("PROXY_MAX_BODY_BYTES", 1048576)
PROXY_MAX_CONCURRENT_PER_DB = _int_env("PROXY_MAX_CONCURRENT_PER_DB", 10)
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
USAGE_SYNC_INTERVAL_SECONDS = _int_env("USAGE_SYNC_INTERVAL_SECONDS", 300)

try:
    Fernet(TURSO_ENC_KEY.encode())
except ValueError as exc:
    raise RuntimeError(
        "TURSO_ENC_KEY is not a valid Fernet key. "
        "Generate one with: python -c \"from cryptography.fernet import Fernet; "
        "print(Fernet.generate_key().decode())\""
    ) from exc


@dataclass(frozen=True)
class PlanLimits:
    """Per-plan quotas. Values come from price.md and are env-overridable."""

    databases: int
    storage_mb: int
    reads_per_month: int
    writes_per_month: int

    @property
    def storage_bytes(self) -> int:
        return self.storage_mb * 1024 * 1024


def _plan(prefix: str, defaults: dict) -> PlanLimits:
    return PlanLimits(
        databases=_int_env(f"PLAN_{prefix}_DATABASES", defaults["databases"]),
        storage_mb=_int_env(f"PLAN_{prefix}_STORAGE_MB", defaults["storage_mb"]),
        reads_per_month=_int_env(f"PLAN_{prefix}_READS", defaults["reads_per_month"]),
        writes_per_month=_int_env(f"PLAN_{prefix}_WRITES", defaults["writes_per_month"]),
    )


PLANS = {
    "free": _plan(
        "FREE",
        {"databases": 1, "storage_mb": 60, "reads_per_month": 5_000_000, "writes_per_month": 75_000},
    ),
    "pro": _plan(
        "PRO",
        {"databases": 1, "storage_mb": 200, "reads_per_month": 10_000_000, "writes_per_month": 400_000},
    ),
}
DEFAULT_PLAN = "free"


def plan_limits(plan: str) -> PlanLimits:
    return PLANS.get(plan, PLANS[DEFAULT_PLAN])
