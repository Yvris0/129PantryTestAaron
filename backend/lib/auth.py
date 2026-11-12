"""Admin PIN session: a signed JWT in an httpOnly cookie."""

import hmac
import os
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import HTTPException, Request, Response

COOKIE = "pantry_session"
MAX_AGE = 60 * 60 * 24 * 7


def _secret() -> str:
    return os.environ["SESSION_SECRET"]


def check_pin(pin: str) -> bool:
    return hmac.compare_digest(pin.strip(), os.environ.get("ADMIN_PIN", ""))


def set_session(response: Response) -> None:
    exp = datetime.now(timezone.utc) + timedelta(seconds=MAX_AGE)
    token = jwt.encode({"role": "admin", "exp": exp}, _secret(), algorithm="HS256")
    response.set_cookie(COOKIE, token, max_age=MAX_AGE, httponly=True, samesite="lax", path="/")


def clear_session(response: Response) -> None:
    response.delete_cookie(COOKIE, path="/")


async def require_admin(request: Request) -> None:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(status_code=401, detail="Not signed in")
    try:
        jwt.decode(token, _secret(), algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Session expired")
