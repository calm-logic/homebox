"""Session-cookie auth backed by ~/.homebox/secrets.json (mounted RO).

The plain admin credentials live on the host in ~/.homebox/secrets.json. We
mount that directory into the container at /host/secrets so the app can read
it directly — no in-app bcrypt verification, no env-var escaping pitfalls.
The same plaintext password is bcrypted into DASHBOARD_AUTH for Traefik's
dashboard basicauth middleware (a separate concern).
"""

import hmac
import json
import time
from typing import Optional

import bcrypt
from fastapi import HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from .config import settings


class RequiresLogin(Exception):
    """Raised by require_session when no valid session is present.
    Caught by an exception handler in main.py and converted to a redirect."""

    def __init__(self, next_path: str = "/"):
        self.next_path = next_path


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.app_secret, salt="homebox-admin-session")


def _load_creds() -> Optional[tuple[str, str]]:
    """Returns (username, bcrypt_hash) from the host-mounted secrets.json,
    or None if the file is missing/malformed."""
    try:
        data = json.loads(settings.homebox_secrets_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError, PermissionError):
        return None
    admin = (data or {}).get("admin") or {}
    user = admin.get("username")
    h = admin.get("password_hash")
    if not user or not h:
        return None
    return user, h


def verify_credentials(username: str, password: str) -> bool:
    creds = _load_creds()
    if not creds:
        return False
    expected_user, expected_hash = creds
    if not hmac.compare_digest(username.encode("utf-8"), expected_user.encode("utf-8")):
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), expected_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def issue_session(response: Response, username: str) -> None:
    token = _serializer().dumps({"u": username, "iat": int(time.time())})
    response.set_cookie(
        settings.session_cookie,
        token,
        max_age=settings.session_max_age_seconds,
        httponly=True,
        secure=False,  # served via Cloudflare tunnel, but tunnel is http://localhost:80
        samesite="lax",
        path="/",
    )


def clear_session(response: Response) -> None:
    response.delete_cookie(settings.session_cookie, path="/")


def _read_session(token: str | None) -> str | None:
    if not token:
        return None
    try:
        data = _serializer().loads(token, max_age=settings.session_max_age_seconds)
    except (BadSignature, SignatureExpired):
        return None
    user = (data or {}).get("u")
    return user if isinstance(user, str) else None


def require_session(request: Request) -> str:
    """Dependency for HTML routes — raises RequiresLogin on failure (handled
    in main.py to redirect to /login with a `next` parameter)."""
    token = request.cookies.get(settings.session_cookie)
    user = _read_session(token)
    if user:
        return user
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    raise RequiresLogin(next_path=target)


def require_session_api(request: Request) -> str:
    """Dependency for JSON/API endpoints — returns 401 instead of redirect."""
    token = request.cookies.get(settings.session_cookie)
    user = _read_session(token)
    if user:
        return user
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
