import hmac
import secrets
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from Auth.services.config import BASE_URL, FRONTEND_URL, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
from Auth.services.limiter import rate_limit
from Auth.services.refresh import issue_tokens
from Database.models import User

router = APIRouter(prefix="/auth/google", tags=["auth"])

REDIRECT_URI = f"{BASE_URL}/auth/google/callback"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"


def fail():
    response = RedirectResponse(f"{FRONTEND_URL}/auth/callback?error=google_failed", status_code=303)
    response.delete_cookie("google_state")
    return response


@router.get("", dependencies=[Depends(rate_limit("google-login", 20, 60))])
def google_login():
    state = secrets.token_urlsafe(24)
    params = urlencode({
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
    })
    response = RedirectResponse(f"{AUTH_URL}?{params}", status_code=303)
    response.set_cookie("google_state", state, httponly=True, samesite="lax", max_age=600, secure=BASE_URL.startswith("https"))
    return response


@router.get("/callback", dependencies=[Depends(rate_limit("google-callback", 20, 60))])
def google_callback(request: Request, code: str = "", state: str = ""):
    saved = request.cookies.get("google_state", "")
    if not code or not state or not saved or not hmac.compare_digest(state, saved):
        return fail()
    try:
        token = httpx.post(TOKEN_URL, timeout=15, data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "grant_type": "authorization_code",
        })
        token.raise_for_status()
        info = httpx.get(USERINFO_URL, timeout=15, headers={
            "Authorization": f"Bearer {token.json()['access_token']}",
        })
        info.raise_for_status()
        info = info.json()
    except (httpx.HTTPError, KeyError, ValueError):
        return fail()
    if not info.get("email") or not info.get("email_verified"):
        return fail()
    email = info["email"].lower()
    user = User.get_or_none(User.google_id == info["sub"]) or User.get_or_none(User.email == email)
    if not user:
        user = User.create(name=info.get("name") or email, email=email, google_id=info["sub"])
    elif not user.google_id:
        user.google_id = info["sub"]
        user.save()
    tokens = issue_tokens(user)
    fragment = urlencode({"access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"]})
    response = RedirectResponse(f"{FRONTEND_URL}/auth/callback#{fragment}", status_code=303)
    response.delete_cookie("google_state")
    return response
