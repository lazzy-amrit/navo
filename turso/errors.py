"""Typed exceptions and their mapping to our HTTP error format.

Every error we return to a customer looks like::

    {"error": {"code": "...", "message": "..."}}
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Base class for every error the API raises deliberately."""

    code = "internal_error"
    status = 500

    def __init__(self, message: str, *, code: str | None = None, status: int | None = None,
                 details: dict | None = None):
        super().__init__(message)
        self.message = message
        if code is not None:
            self.code = code
        if status is not None:
            self.status = status
        self.details = details or {}

    def to_payload(self) -> dict:
        error: dict = {"code": self.code, "message": self.message}
        if self.details:
            error["details"] = self.details
        return {"error": error}


# --- customer/domain errors -------------------------------------------------


class ValidationError(AppError):
    code = "invalid_request"
    status = 400


class AliasInvalid(ValidationError):
    code = "invalid_alias"


class NotFoundError(AppError):
    code = "not_found"
    status = 404


class ForbiddenError(AppError):
    code = "forbidden"
    status = 403


class AliasTaken(AppError):
    code = "alias_taken"
    status = 409


class PlanLimitReached(AppError):
    code = "plan_limit_reached"
    status = 403


class CapacityReached(AppError):
    code = "capacity_reached"
    status = 503


class QuotaExceeded(AppError):
    code = "quota_exceeded"
    status = 429


class TokenInvalid(AppError):
    code = "invalid_token"
    status = 401


class DatabaseUnavailable(AppError):
    code = "database_unavailable"
    status = 403


class BodyTooLarge(AppError):
    code = "body_too_large"
    status = 413


class TooManyRequests(AppError):
    code = "too_many_requests"
    status = 429


class SqlError(AppError):
    code = "sql_error"
    status = 400


class ProvisioningFailed(AppError):
    code = "provisioning_failed"
    status = 502


class UpstreamError(AppError):
    """Turso could not be reached or answered with an error we cannot map."""

    code = "upstream_error"
    status = 502


# --- upstream (Turso Platform API) errors -----------------------------------


class TursoError(AppError):
    code = "turso_error"
    status = 502


class TursoNotFound(TursoError):
    code = "turso_not_found"
    status = 404


class TursoConflict(TursoError):
    code = "turso_conflict"
    status = 409


class TursoAuthError(TursoError):
    code = "turso_auth_error"
    status = 502


class TursoPaymentRequired(TursoError):
    code = "turso_payment_required"
    status = 503


class TursoRateLimited(TursoError):
    code = "turso_rate_limited"
    status = 503


class TursoUpstreamError(TursoError):
    code = "turso_upstream_error"
    status = 502


def app_error_handler(request: Request, exc: AppError) -> JSONResponse:
    """FastAPI exception handler emitting our standard error envelope."""
    return JSONResponse(status_code=exc.status, content=exc.to_payload())


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
