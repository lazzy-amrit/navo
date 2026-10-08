"""HTML rendering helpers. Server-rendered pages — not a SPA."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.requests import Request

from config import settings
from security import ensure_csrf

templates = Jinja2Templates(directory="templates")


def fmt_int(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "0"


def fmt_mb(bytes_value) -> str:
    try:
        mb = int(bytes_value) / (1024 * 1024)
    except (TypeError, ValueError):
        return "0 MB"
    if mb < 0.1 and int(bytes_value or 0) > 0:
        return f"{int(bytes_value) / 1024:.1f} KB"
    if mb < 10:
        return f"{mb:.1f} MB"
    return f"{mb:.0f} MB"


def pct(used, limit) -> int:
    try:
        if not limit:
            return 0
        value = int(round((float(used) / float(limit)) * 100))
        return max(0, min(100, value))
    except (TypeError, ValueError, ZeroDivisionError):
        return 0


templates.env.filters["intcomma"] = fmt_int
templates.env.filters["mb"] = fmt_mb
templates.env.filters["pct"] = pct
templates.env.globals["fmt_int"] = fmt_int
templates.env.globals["fmt_mb"] = fmt_mb
templates.env.globals["pct"] = pct


def pop_flash(request: Request) -> dict | None:
    flash = request.session.pop("flash", None)
    if isinstance(flash, dict):
        return flash
    return None


def set_flash(request: Request, kind: str, message: str) -> None:
    request.session["flash"] = {"kind": kind, "message": message}


def current_user(request: Request):
    return getattr(request.state, "user", None)


def render(request: Request, template: str, status_code: int = 200, **context) -> HTMLResponse:
    context.setdefault("request", request)
    context.setdefault("user", current_user(request))
    context.setdefault("csrf_token", ensure_csrf(request))
    context.setdefault("flash", pop_flash(request))
    context.setdefault("public", settings.public())
    context.setdefault("nav", "")
    context["now"] = datetime.now(timezone.utc)
    return templates.TemplateResponse(request, template, context, status_code=status_code)


def redirect(url: str, status_code: int = 303) -> RedirectResponse:
    return RedirectResponse(url, status_code=status_code)
