from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import (
    clear_session, issue_session, require_session_api, verify_credentials,
    verify_password_only,
)
from ..db import get_session
from ..models import Identity
from .oauth import _proxy_providers

router = APIRouter(prefix="/api/auth")


class LoginBody(BaseModel):
    # Username is optional (G3): the fresh-install login form is password-only
    # (there is a single fixed admin user). An explicit username still works
    # for back-compat with older frontends/scripts.
    username: str | None = None
    password: str


@router.post("/login")
async def login(body: LoginBody, response: Response):
    username = (body.username or "").strip()
    if username:
        if not verify_credentials(username, body.password):
            raise HTTPException(status_code=401, detail="Invalid username or password")
    else:
        username = verify_password_only(body.password) or ""
        if not username:
            raise HTTPException(status_code=401, detail="Invalid password")
    issue_session(response, username)
    return {"ok": True, "username": username}


@router.get("/login-options")
async def login_options(session: AsyncSession = Depends(get_session)):
    """Unauthenticated — everything the login page needs to render itself:
    which OAuth providers the proxy can drive, and whether any enabled
    Identity exists yet (fresh installs show the "paste the installer
    password" hint and skip the OAuth buttons — nobody could pass the
    whitelist check anyway)."""
    providers = await _proxy_providers()
    has_identities = (
        await session.execute(
            select(Identity.id).where(Identity.enabled == True).limit(1)  # noqa: E712
        )
    ).first() is not None
    return {
        "oauth_providers": [p for p in ("github", "google") if providers.get(p)],
        "has_identities": has_identities,
    }


@router.post("/logout")
async def logout(response: Response):
    clear_session(response)
    return {"ok": True}


@router.get("/me")
async def me(request: Request):
    user = require_session_api(request)
    return {"username": user}
