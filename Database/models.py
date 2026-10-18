"""Metadata models shared by the app.

Auth imports ``User``, ``RefreshToken``, ``RateLimit`` and ``Otp`` from here.
The only additions beyond what Auth needs are ``User.plan`` (source of truth:
price.md) and ``User.is_admin`` (needed for the /admin routes).
"""

from peewee import (
    BooleanField,
    CharField,
    DateTimeField,
    ForeignKeyField,
    IntegerField,
    Model,
)

from Database import db, utcnow


class BaseModel(Model):
    class Meta:
        database = db


class User(BaseModel):
    name = CharField(max_length=80)
    email = CharField(max_length=255, unique=True)
    password_hash = CharField(null=True)
    google_id = CharField(max_length=64, null=True, unique=True)
    plan = CharField(max_length=16, default="free", index=True)
    is_admin = BooleanField(default=False)
    created_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "user"


class RefreshToken(BaseModel):
    user = ForeignKeyField(User, backref="refresh_tokens", on_delete="CASCADE")
    token_hash = CharField(max_length=64, unique=True)
    expires_at = DateTimeField()
    revoked = BooleanField(default=False)
    created_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "refresh_token"


class RateLimit(BaseModel):
    key = CharField(max_length=255, unique=True)
    count = IntegerField(default=0)
    reset_at = DateTimeField()

    class Meta:
        table_name = "rate_limit"


class Otp(BaseModel):
    user = ForeignKeyField(User, backref="otps", on_delete="CASCADE")
    code_hash = CharField(max_length=64)
    attempts = IntegerField(default=0)
    expires_at = DateTimeField()
    verified_at = DateTimeField(null=True)
    created_at = DateTimeField(default=utcnow)

    class Meta:
        table_name = "otp"
