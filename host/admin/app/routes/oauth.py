"""OAuth via the homebox.sh oauth-proxy — two purposes share one round-trip.

The proxy holds the central OAuth client(s). We sign an opaque `state` (carrying
the purpose + provider), redirect the browser to the proxy, and the proxy
redirects back to https://<this host>/oauth/callback?code=<access_token>&state=…
(`code` is the access_token; we keep the name `code` for a single callback shape
on the SPA route). The SPA then POSTs to /api/oauth/finish.

Purposes:
  connect — enumerate the user's GitHub orgs and store one github Integration
            row per org (access_token encrypted at rest). Requires a session.
  login   — resolve a verified email from the provider and match it against the
            `identities` whitelist. No prior session (this *is* the sign-in). On
            a match we issue an admin session bound to that email; otherwise the
            login is rejected.
"""

import socket
from datetime import datetime
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .. import cluster_sync, clusterlib, github
from ..auth import issue_session, require_session_api, _read_session
from ..config import settings
from ..crypto import decrypt, encrypt
from ..db import get_session
from ..models import Identity, Integration, Project, Setting
from ..integrations_lib import sync_github_projects

router = APIRouter(prefix="/api/oauth")

OAUTH_STATE_COOKIE = "homebox_oauth_state"
OAUTH_STATE_TTL_SEC = 600  # 10 minutes
LOGIN_PROVIDERS = ("github", "google")
ADMIN_DOMAIN_KEY = "admin_domain"  # Setting written by onboarding (routes/onboarding.py)


def _state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.app_secret, salt="homebox-oauth-state")


async def _installation_url(request: Request, session: AsyncSession) -> str:
    """The public origin of this install — the proxy redirects the browser back
    here on /oauth/callback, so it MUST be the externally reachable URL.

    Prefer the canonical admin hostname chosen during onboarding. The raw `Host`
    header is unreliable here: depending on the tunnel/proxy hops, the request
    can arrive with `Host: 127.0.0.1:7765` (the admin's localhost bind), which
    would send the OAuth callback to a dead address. Fall back to the forwarded
    host, then the raw Host, only when onboarding hasn't set a domain yet."""
    row = (
        await session.execute(select(Setting).where(Setting.key == ADMIN_DOMAIN_KEY))
    ).scalar_one_or_none()
    domain = row.value if row else None
    if isinstance(domain, str) and domain.strip():
        return f"https://{domain.strip().strip('/')}"

    fwd_proto = request.headers.get("x-forwarded-proto", "https")
    fwd_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    host = fwd_host.split(",")[0].strip()  # X-Forwarded-Host may be a list
    return f"{fwd_proto}://{host}"


def _start_url(state: str, provider: str, purpose: str, installation: str) -> str:
    """The proxy /start URL carrying the signed state, provider and purpose."""
    qs = urlencode({
        "installation": installation,
        "state": state,
        "provider": provider,
        "purpose": purpose,
    })
    return f"{settings.homebox_oauth_proxy_url}/start?{qs}"


def _set_state_cookie(response: Response, state: str) -> None:
    """Persist the single-use CSRF state so /finish can match it on return."""
    response.set_cookie(
        OAUTH_STATE_COOKIE, state,
        max_age=OAUTH_STATE_TTL_SEC, httponly=True, samesite="lax", path="/",
    )


def _proxy_redirect(response: Response, state: str, provider: str, purpose: str, installation: str) -> dict:
    """Set the CSRF state cookie and point the response at the proxy /start."""
    target = _start_url(state, provider, purpose, installation)
    response.headers["location"] = target
    response.status_code = 302
    _set_state_cookie(response, state)
    return {"redirect": target}


async def installation_url(request: Request, session: AsyncSession) -> str:
    """Public wrapper around the callback origin resolver (see _installation_url)."""
    return await _installation_url(request, session)


def build_account_link_start(response: Response, provider: str, user: str, installation: str) -> str:
    """Mint a signed ACCOUNT-LINK state, set the CSRF cookie on `response`, and
    return the proxy /start URL for a caller that wants to open it in a popup
    (rather than 302-redirect). The proxy `purpose` stays `login` so it hands
    back the raw provider access token as `code`; the node distinguishes this
    from an actual login purely by the state's `mode` field."""
    state = _state_serializer().dumps(
        {"purpose": "login", "provider": provider, "mode": "account-link", "u": user}
    )
    _set_state_cookie(response, state)
    return _start_url(state, provider, "login", installation)


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
    session: AsyncSession = Depends(get_session),
):
    """Redirect to the proxy to connect GitHub orgs for deployment."""
    state = _state_serializer().dumps({"purpose": "connect", "provider": "github", "u": user})
    installation = await _installation_url(request, session)
    return _proxy_redirect(response, state, "github", "connect", installation)


# ───── Start: passwordless login ──────────────────────────────────────────────

@router.get("/login/{provider}/start")
async def login_start(
    provider: str,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
):
    """Unauthenticated — begin a passwordless login via the given provider."""
    provider = provider.lower()
    if provider not in LOGIN_PROVIDERS:
        raise HTTPException(404, "Unknown login provider")
    state = _state_serializer().dumps({"purpose": "login", "provider": provider})
    installation = await _installation_url(request, session)
    return _proxy_redirect(response, state, provider, "login", installation)


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
    mode = payload.get("mode")

    # ACCOUNT-LINK rides the login round-trip (proxy purpose=login → raw access
    # token in `code`) but is a distinct flow, chosen by the state's `mode`.
    if mode == "account-link":
        return await _finish_account_link(provider, access_token, request, session)
    if purpose == "login":
        return await _finish_login(provider, access_token, response, session)
    return await _finish_connect(access_token, request, session)


async def _finish_account_link(provider: str, access_token: str, request: Request, session: AsyncSession) -> dict:
    """Register (or re-authenticate) a homebox.sh account from a provider access
    token and link this node to it. Never raises: on failure it returns a
    browser redirect to /system carrying an `account_error` so the SPA can show
    it inline, matching the success redirect to /system?account=linked."""

    def _err(msg: str) -> dict:
        return {"ok": False, "purpose": "account-link",
                "redirect": f"/system?account_error={quote(msg)}"}

    # Linking an account is privileged — require an existing admin session.
    if not _read_session(request.cookies.get(settings.session_cookie)):
        return _err("Sign in before linking an account.")

    # Control plane: the active cluster's stored URL if any, else the default.
    state = await clusterlib.load_cluster(session)
    acct = await clusterlib.load_account(session)
    cp_url = (state.get("control_plane_url") if state else None) or settings.homebox_control_plane_url

    # Node identity for the account label + node registration. Prefer any name
    # already chosen for this node (account/cluster), else the OS hostname.
    node_name = (
        (acct.get("node_name") if acct else None)
        or (state.get("node_name") if state else None)
        or socket.gethostname()
        or "homebox"
    )
    peer_url = (acct.get("peer_url") if acct else None) or (state.get("peer_url") if state else None) or ""

    try:
        reg = await clusterlib._cp(
            "POST", cp_url, "/v1/accounts/register",
            body={"provider": provider, "access_token": access_token, "label": f"node {node_name}"},
        )
    except clusterlib.ControlPlaneError as e:
        if e.status_code == 401:
            return _err("Provider sign-in was rejected. Please try again.")
        if e.status_code == 502:
            return _err(f"Couldn't reach {provider.title()}. Please try again.")
        return _err(e.detail or "Account sign-in failed.")

    account_token = (reg.get("account_token") or "").strip()
    if not account_token:
        return _err("The account service returned no token.")

    try:
        await clusterlib.link_account_flow(
            session,
            control_plane_url=cp_url,
            account_token_plain=account_token,
            node_name=node_name,
            peer_url=peer_url,
        )
    except clusterlib.ControlPlaneError as e:
        return _err(e.detail or "Linking this node to the account failed.")

    return {"ok": True, "purpose": "account-link", "redirect": "/system?account=linked"}


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
    # Connecting GitHub is privileged — require an existing admin session.
    if not _read_session(request.cookies.get(settings.session_cookie)):
        raise HTTPException(401, "Sign in before connecting GitHub.")

    # ONE integration per GitHub identity: the account's own repos plus every
    # org that granted the OAuth app access, all behind a single token. (Orgs
    # are granted on GitHub's consent screen — re-connecting after granting
    # more orgs just refreshes this same row.)
    try:
        gh_user = await github.get_user(access_token)
    except httpx.HTTPStatusError as e:
        raise HTTPException(400, f"GitHub /user returned {e.response.status_code}")
    login = gh_user.get("login") or ""
    if not login:
        raise HTTPException(400, "GitHub returned no account login for this token.")
    try:
        orgs = [o.get("login") for o in await github.list_user_orgs(access_token) if o.get("login")]
    except httpx.HTTPStatusError:
        orgs = []

    encrypted = encrypt(f"oauth:{access_token}")
    integ = (await session.execute(
        select(Integration).where(Integration.provider == "github", Integration.account_login == login)
    )).scalar_one_or_none()
    if integ:
        integ.secret_encrypted = encrypted
        integ.status = "connected"
        integ.updated_at = datetime.utcnow()
        integ.config = {**(integ.config or {}), "scope": "account", "orgs": orgs}
    else:
        integ = Integration(
            provider="github", account_login=login, account_id=str(gh_user.get("id") or ""),
            name=login, secret_encrypted=encrypted, status="connected",
            config={"scope": "account", "orgs": orgs},
        )
        session.add(integ)
    await session.flush()

    # Consolidate legacy per-org rows from the old connect flow: they held this
    # same OAuth token (one copy per org), so their projects move to the
    # account row losslessly. PAT rows keep their own credentials — untouched.
    legacy = (await session.execute(
        select(Integration).where(Integration.provider == "github", Integration.id != integ.id)
    )).scalars().all()
    tombs: list[tuple[str, list]] = []
    for old in legacy:
        if not (decrypt(old.secret_encrypted or "") or "").startswith("oauth:"):
            continue  # PAT-connected org — its own credential, leave it alone
        await session.execute(
            update(Project).where(Project.integration_id == old.id)
            .values(integration_id=integ.id)
        )
        tombs.append(("integration", [old.provider, old.account_login]))
        await session.delete(old)
    if tombs:
        await cluster_sync.record_tombstones(session, tombs, commit=False)
    await session.commit()

    # Auto-sync repos so projects show up immediately (best-effort).
    try:
        await sync_github_projects(session, integ)
        await session.commit()
    except httpx.HTTPStatusError:
        await session.rollback()

    return {"ok": True, "purpose": "connect", "redirect": "/projects", "orgs": [login, *orgs]}


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
