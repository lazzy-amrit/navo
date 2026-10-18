import datetime as dt
import hashlib
import random

from fastapi import HTTPException, Request

from Auth.services.config import utcnow
from Database.models import RateLimit


def client_ip(request: Request):
    return request.client.host if request.client else "unknown"


def hash_value(value):
    return hashlib.sha256(value.lower().encode()).hexdigest()[:32]


def check_limit(key, max_requests, window_seconds):
    now = utcnow()
    row = RateLimit.get_or_none(RateLimit.key == key)
    if not row or row.reset_at <= now:
        RateLimit.insert(
            key=key,
            count=1,
            reset_at=now + dt.timedelta(seconds=window_seconds),
        ).on_conflict_replace().execute()
    elif row.count >= max_requests:
        retry = int((row.reset_at - now).total_seconds()) + 1
        raise HTTPException(
            429,
            "Too many attempts. Try again later.",
            headers={"Retry-After": str(retry)},
        )
    else:
        RateLimit.update(count=RateLimit.count + 1).where(RateLimit.key == key).execute()
    if random.random() < 0.01:
        RateLimit.delete().where(RateLimit.reset_at < now).execute()


def rate_limit(name, max_requests, window_seconds):
    def dependency(request: Request):
        check_limit(f"{name}:ip:{client_ip(request)}", max_requests, window_seconds)

    return dependency


def check_email_limit(name, email, max_requests, window_seconds):
    check_limit(f"{name}:email:{hash_value(email)}", max_requests, window_seconds)


def check_user_limit(name, user_id, max_requests, window_seconds):
    check_limit(f"{name}:user:{user_id}", max_requests, window_seconds)
