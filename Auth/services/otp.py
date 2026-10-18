import datetime as dt
import hashlib
import hmac
import secrets

from fastapi import HTTPException

from Auth.services.config import (
    OTP_MAX_ATTEMPTS,
    OTP_MINUTES,
    OTP_VERIFIED_MINUTES,
    SECRET_KEY,
    utcnow,
)
from Database.models import Otp


def hash_code(code):
    return hmac.new(SECRET_KEY.encode(), code.encode(), hashlib.sha256).hexdigest()


def generate_otp(user):
    Otp.delete().where(Otp.user == user).execute()
    code = f"{secrets.randbelow(1_000_000):06d}"
    Otp.create(
        user=user,
        code_hash=hash_code(code),
        expires_at=utcnow() + dt.timedelta(minutes=OTP_MINUTES),
    )
    return code


def verify_otp(user, code):
    row = Otp.get_or_none(Otp.user == user)
    if not row or row.expires_at < utcnow() or row.attempts >= OTP_MAX_ATTEMPTS:
        raise HTTPException(400, "Invalid or expired code")
    if not hmac.compare_digest(row.code_hash, hash_code(code)):
        row.attempts += 1
        row.save()
        raise HTTPException(400, "Invalid or expired code")
    row.verified_at = utcnow()
    row.save()


def is_otp_verified(user):
    row = Otp.get_or_none(Otp.user == user)
    if not row or not row.verified_at:
        return False
    return row.verified_at + dt.timedelta(minutes=OTP_VERIFIED_MINUTES) > utcnow()


def clear_otp(user):
    Otp.delete().where(Otp.user == user).execute()
