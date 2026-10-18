import datetime as dt
import hashlib
import secrets

from fastapi import HTTPException

from Auth.services.config import REFRESH_DAYS, utcnow
from Auth.services.jwt import create_access_token
from Database.models import RefreshToken


def hash_token(token):
    return hashlib.sha256(token.encode()).hexdigest()


def create_refresh_token(user):
    raw = secrets.token_urlsafe(48)
    RefreshToken.create(
        user=user,
        token_hash=hash_token(raw),
        expires_at=utcnow() + dt.timedelta(days=REFRESH_DAYS),
    )
    return raw


def get_valid_refresh(raw):
    row = RefreshToken.get_or_none(RefreshToken.token_hash == hash_token(raw))
    if not row or row.revoked or row.expires_at < utcnow():
        raise HTTPException(401, "Invalid or expired refresh token")
    return row


def revoke_refresh_token(raw):
    RefreshToken.update(revoked=True).where(RefreshToken.token_hash == hash_token(raw)).execute()


def revoke_all_refresh_tokens(user):
    RefreshToken.update(revoked=True).where(RefreshToken.user == user).execute()


def issue_tokens(user):
    return {
        "access_token": create_access_token(user.id),
        "refresh_token": create_refresh_token(user),
        "token_type": "bearer",
    }


def refresh_session(raw, kind):
    row = get_valid_refresh(raw)
    user = row.user
    if kind == "access":
        return {"access_token": create_access_token(user.id), "token_type": "bearer"}
    row.revoked = True
    row.save()
    return issue_tokens(user)
