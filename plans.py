"""Plan limits. Enforced only on the server. Do not invent different numbers."""

PLANS = {
    "free": {
        "storage_mb": 60,
        "reads_per_month": 5_000_000,
        "writes_per_month": 75_000,
        "databases": 1,
    },
    "pro": {
        "storage_mb": 200,
        "reads_per_month": 10_000_000,
        "writes_per_month": 400_000,
        "databases": 1,
    },
}

PLAN_LABELS = {
    "free": "Free",
    "pro": "Pro",
}


def get_plan(name: str) -> dict:
    return PLANS.get(name, PLANS["free"])


def storage_limit_bytes(plan_name: str) -> int:
    return int(get_plan(plan_name)["storage_mb"]) * 1024 * 1024
