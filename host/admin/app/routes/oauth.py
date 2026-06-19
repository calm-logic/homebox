"""OAuth via the homebox.sh oauth-proxy — two purposes share one round-trip.

The proxy holds the central OAuth client(s). We sign an opaque `state` (carrying
the purpose + provider), redirect the browser to the proxy, and the proxy
redirects back to https://<this host>/oauth/callback?code=<access_token>&state=…
(`code` is the access_token; we keep the name `code` for a single callback shape
on the SPA route). The SPA then POSTs to /api/oauth/finish.

Purposes:
  connect — enumerate the user's GitHub orgs and store one Organization row per
            org (access_token encrypted at rest). Requires an existing session.
  login   — resolve a verified email from the provider and match it against the
            `identities` whitelist. No prior session (this *is* the sign-in). On
            a match we issue an admin session bound to that email; otherwise the
            login is rejected.
"""

from datetime import datetime
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import issue_session, require_session_api, _read_session
from ..config import settings
from ..crypto import encrypt
from ..db import get_session
from ..models import Identity, Organization
from ..orgs import sync_org_repos

router = APIRouter(prefix="/api/oauth")

OAUTH_STATE_COOKIE = "homebox_oauth_state"
OAUTH_STATE_TTL_SEC = 600  # 10 minutes
LOGIN_PROVIDERS = ("github", "google")


def _state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.app_secret, salt="homebox-oauth-state")


def _installation_url(request: Request) -> str:
    """The public origin of this install — the proxy redirects the browser back
    here on /oauth/callback. Built from the forwarded Host (we sit behind Traefik
    + Cloudflare)."""
    fwd_proto = request.headers.get("x-forwarded-proto", "https")
    host = request.headers.get("host") or ""
    return f"{fwd_proto}://{host}"


def _proxy_redirect(response: Response, state: str, provider: str, purpose: str, installation: str) -> dict:
    """Set the CSRF state cookie and point the response at the proxy /start."""
    qs = urlencode({
        "installation": installation,
        "state": state,
        "provider": provider,
        "purpose": purpose,
    })
    target = f"{settings.homebox_oauth_proxy_url}/start?{qs}"
    response.headers["location"] = target
    response.status_code = 302
    response.set_cookie(
        OAUTH_STATE_COOKIE, state,
        max_age=OAUTH_STATE_TTL_SEC, httponly=True, samesite="lax", path="/",
    )
    return {"redirect": target}


# ───── Provider/settings probes ───────────────────────────────────────────────

async def _proxy_providers() -> dict:
    try:
        async with httpx.AsyncClient(timeout=4) as c:
            r = await c.get(f"{settings.homebox_oauth_proxy_url}/providers")
            if r.status_code == 200:
                data = r.json()
                return {"github": bool(data.get("github")), "google": bool(data.get("google"))}
    except (httpx.HTTPError, OSError, ValueError):
        pass
    return {"github": False, "google": False}


@router.get("/login-providers")
async def login_providers():
    """Unauthenticated — the login page renders its OAuth buttons from this."""
    return await _proxy_providers()


@router.get("/settings")
async def oauth_settings(user: str = Depends(require_session_api)):
    """Probe the proxy to see whether it's reachable (used by the connect page)."""
    providers = await _proxy_providers()
    return {
        "configured": providers["github"],
        "providers": providers,
        "proxy_url": settings.homebox_oauth_proxy_url,
        "client_id": None,  # Held by the proxy; opaque to admin
    }


# ───── Start: connect (GitHub orgs) ───────────────────────────────────────────

@router.get("/github/start")
async def connect_start(
    request: Request,
    response: Response,
    user: str = Depends(require_session_api),
):
    """Redirect to the proxy to connect GitHub orgs for deployment."""
    state = _state_serializer().dumps({"purpose": "connect", "provider": "github", "u": user})
    return _proxy_redirect(response, state, "github", "connect", _installation_url(request))


# ───── Start: passwordless login ──────────────────────────────────────────────

@router.get("/login/{provider}/start")
async def login_start(provider: str, request: Request, response: Response):
    """Unauthenticated — begin a passwordless login via the given provider."""
    provider = provider.lower()
    if provider not in LOGIN_PROVIDERS:
        raise HTTPException(404, "Unknown login provider")
    state = _state_serializer().dumps({"purpose": "login", "provider": provider})
    return _proxy_redirect(response, state, provider, "login", _installation_url(request))


# ───── Finish (unified) ───────────────────────────────────────────────────────

class FinishBody(BaseModel):
    code: str   # access token (issued by the proxy after exchange)
    state: str


@router.post("/finish")
async def oauth_finish(
    body: FinishBody,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    cookie_state = request.cookies.get(OAUTH_STATE_COOKIE)
    if not cookie_state or cookie_state != body.state:
        raise HTTPException(400, "OAuth state mismatch — please start again.")
    try:
        payload = _state_serializer().loads(body.state, max_age=OAUTH_STATE_TTL_SEC)
    except BadSignature:
        raise HTTPException(400, "Invalid OAuth state")

    access_token = body.code.strip()
    if not access_token:
        raise HTTPException(400, "Missing access token")

    # Clear the single-use state cookie regardless of branch outcome.
    response.delete_cookie(OAUTH_STATE_COOKIE, path="/")

    purpose = payload.get("purpose", "connect")
    provider = (payload.get("provider") or "github").lower()

    if purpose == "login":
        return await _finish_login(provider, access_token, response, session)
    return await _finish_connect(access_token, request, session)


async def _finish_login(provider: str, access_token: str, response: Response, session: AsyncSession) -> dict:
    email = await _resolve_verified_email(provider, access_token)
    if not email:
        raise HTTPException(400, "Could not read a verified email from your account.")

    identity = (
        await session.execute(select(Identity).where(Identity.email == email))
    ).scalar_one_or_none()
    if identity is None or not identity.enabled:
        # Same message for unknown and disabled — don't leak which emails exist.
        raise HTTPException(403, "This email is not authorized to access Homebox.")

    identity.last_login_at = datetime.utcnow()
    identity.last_login_provider = provider
    identity.login_count = (identity.login_count or 0) + 1
    await session.commit()

    issue_session(response, identity.email)
    return {"ok": True, "purpose": "login", "redirect": "/"}


async def _finish_connect(access_token: str, request: Request, session: AsyncSession) -> dict:
    # Connecting orgs is privileged — require an existing admin session.
    if not _read_session(request.cookies.get(settings.session_cookie)):
        raise HTTPException(401, "Sign in before connecting a GitHub organization.")

    # Fetch the user's organizations + add each as an Organization row.
    async with httpx.AsyncClient(timeout=15) as c:
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {access_token}",
            "User-Agent": "homebox-admin",
        }
        r = await c.get("https://api.github.com/user/orgs", headers=headers, params={"per_page": 100})
        if r.status_code != 200:
            raise HTTPException(400, f"GitHub /user/orgs returned {r.status_code}")
        orgs = r.json() or []

    if not orgs:
        raise HTTPException(400, "No organizations are visible to this token. Make sure the OAuth app is installed and approved for at least one org.")

    encrypted = encrypt(f"oauth:{access_token}")
    added = []
    org_rows: list[Organization] = []
    for o in orgs:
        login = o.get("login")
        if not login:
            continue
        existing = (await session.execute(select(Organization).where(Organization.login == login))).scalar_one_or_none()
        if existing:
            existing.pat_encrypted = encrypted
        else:
            existing = Organization(login=login, pat_encrypted=encrypted)
            session.add(existing)
        org_rows.append(existing)
        added.append(login)
    await session.commit()

    # Auto-sync repos for each connected org so projects show up immediately.
    # Best-effort per org — a single failure shouldn't sink the whole connect.
    for org in org_rows:
        try:
            await sync_org_repos(session, org)
            await session.commit()
        except httpx.HTTPStatusError:
            await session.rollback()

    return {"ok": True, "purpose": "connect", "redirect": "/projects", "orgs": added}


# ───── Provider email resolution ──────────────────────────────────────────────

async def _resolve_verified_email(provider: str, access_token: str) -> str | None:
    """Return the lowercased, provider-verified email for the token, or None."""
    if provider == "google":
        async with httpx.AsyncClient(timeout=15) as c:
            r = await c.get(
                "https://www.googleapis.com/oauth2/v3/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if r.status_code != 200:
                return None
            data = r.json() or {}
        email = (data.get("email") or "").strip().lower()
        verified = data.get("email_verified")
        # Google returns email_verified as bool or "true"/"false" string.
        if email and (verified is True or str(verified).lower() == "true"):
            return email
        return None

    # GitHub — pick the primary verified email.
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            "https://api.github.com/user/emails",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "homebox-admin",
            },
        )
        if r.status_code != 200:
            return None
        emails = r.json() or []
    primary = next((e for e in emails if e.get("primary") and e.get("verified")), None)
    chosen = primary or next((e for e in emails if e.get("verified")), None)
    if chosen and chosen.get("email"):
        return chosen["email"].strip().lower()
    return None
