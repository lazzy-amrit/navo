import datetime as dt

import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from Auth.services.config import ACCESS_MINUTES, SECRET_KEY
from Database.models import User

bearer = HTTPBearer(auto_error=False)


def create_access_token(user_id):
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": str(user_id),
        "type": "access",
        "iat": now,
        "exp": now + dt.timedelta(minutes=ACCESS_MINUTES),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def decode_access_token(token):
    try:
        data = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Invalid or expired token")
    if data.get("type") != "access":
        raise HTTPException(401, "Invalid or expired token")
    return data


def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(bearer)):
    if not credentials:
        raise HTTPException(401, "Not authenticated")
    data = decode_access_token(credentials.credentials)
    user = User.get_or_none(User.id == int(data["sub"]))
    if not user:
        raise HTTPException(401, "Not authenticated")
    return user
