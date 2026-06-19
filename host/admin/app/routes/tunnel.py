"""Tunnel + Cloudflare credentials API.

Cloudflared runs in remotely-managed mode only: just a connector token, no
config.yml/credentials JSON on disk, ingress rules pushed via the Cloudflare
API. Set up by the admin's onboarding wizard; this module exposes the status
+ lifecycle endpoints the UI calls into.
"""

import secrets
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import cloudflare as cf
from ..auth import require_session_api
from ..db import get_session
from ..models import Domain, Setting
from ..host import (
    container_status,
    remove_container,
    restart_container,
    run_cloudflared_remote,
)

router = APIRouter(prefix="/api/tunnel")

INSTALL_ID_KEY = "install_id"


async def _get_install_id(session: AsyncSession) -> str:
    """Return this Homebox install's stable random identifier, generating it
    on first call. Used as `metadata.homebox_install_id` on every tunnel we
    create so we can recognize our own on re-runs."""
    row = (await session.execute(select(Setting).where(Setting.key == INSTALL_ID_KEY))).scalar_one_or_none()
    if row and isinstance(row.value, dict) and row.value.get("value"):
        return str(row.value["value"])
    new_id = secrets.token_urlsafe(16)
    if row is None:
        session.add(Setting(key=INSTALL_ID_KEY, value={"value": new_id}))
    else:
        row.value = {"value": new_id}
    await session.commit()
    return new_id


def _is_ours(tunnel: dict[str, Any], install_id: str) -> bool:
    md = tunnel.get("metadata") or {}
    return isinstance(md, dict) and md.get("homebox_install_id") == install_id


def _connector_count(tunnel: dict[str, Any]) -> int:
    conns = tunnel.get("connections") or []
    return len(conns) if isinstance(conns, list) else 0


def _tunnel_metadata(install_id: str) -> dict[str, str]:
    return {"homebox": "1", "homebox_install_id": install_id}


def _serialize_domain(d: Domain) -> dict[str, Any]:
    return {
        "id": d.id, "name": d.name, "mode": d.mode,
        "project_slug": d.project_slug, "is_primary": d.is_primary,
        "cloudflare_routed": d.cloudflare_routed,
    }


async def _push_ingress(state: dict[str, Any], session: AsyncSession) -> None:
    token = cf.get_token(state)
    if not token or not state.get("account_id") or not state.get("tunnel_id"):
        return
    domains = (
        await session.execute(select(Domain).order_by(Domain.name))
    ).scalars().all()
    ingress = cf.build_ingress([{"name": d.name} for d in domains])
    await cf.put_tunnel_config(token, state["account_id"], state["tunnel_id"], ingress)


# ───── Status ─────────────────────────────────────────────────────────────────


@router.get("")
async def tunnel_view(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    state = await cf.load_state(session)
    domains = (await session.execute(select(Domain).order_by(Domain.name))).scalars().all()
    cf_status = container_status("homebox-cloudflared")

    tunnel_id = state.get("tunnel_id")
    return {
        "exists": cf_status.get("exists", False),
        "running": cf_status.get("running", False),
        "state": cf_status.get("state", "unknown"),
        "mode": "remote" if tunnel_id else "none",
        "tunnel_id": tunnel_id,
        "tunnel_name": state.get("tunnel_name"),
        "cloudflare": {
            "token_set": bool(cf.get_token(state)),
            "account_id": state.get("account_id"),
            "account_name": state.get("account_name"),
        },
        "domains": [_serialize_domain(d) for d in domains],
    }


# ───── Cloudflare API token connect ───────────────────────────────────────────


class TokenBody(BaseModel):
    token: str
    account_id: str | None = None


@router.post("/token")
async def set_cloudflare_token(
    body: TokenBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    token = body.token.strip()
    if not token:
        raise HTTPException(400, "API token is required")

    try:
        verify = await cf.verify_token(token)
    except cf.CloudflareError as e:
        raise HTTPException(400, f"Token rejected by Cloudflare: {e}")

    if (verify.get("status") or "").lower() != "active":
        raise HTTPException(400, "Token is not active. Generate a new token in the Cloudflare dashboard.")

    try:
        accounts = await cf.list_accounts(token)
    except cf.CloudflareError as e:
        raise HTTPException(
            400,
            f"Token can't list accounts ({e}). Ensure the token has 'Account Settings: Read' "
            "in addition to Tunnel and DNS permissions.",
        )

    if not accounts:
        raise HTTPException(400, "Token has no accessible Cloudflare accounts.")

    state = await cf.load_state(session)
    cf.store_token(state, token)

    account_id = body.account_id
    if account_id:
        match = next((a for a in accounts if a.get("id") == account_id), None)
        if not match:
            raise HTTPException(400, "Selected account is not visible to this token.")
        state["account_id"] = account_id
        state["account_name"] = match.get("name") or ""
    elif len(accounts) == 1:
        state["account_id"] = accounts[0].get("id")
        state["account_name"] = accounts[0].get("name") or ""
    # If multiple accounts and none chosen, leave unset — UI will prompt.

    await cf.save_state(session, state)

    return {
        "ok": True,
        "accounts": [{"id": a.get("id"), "name": a.get("name")} for a in accounts],
        "account_id": state.get("account_id"),
        "account_name": state.get("account_name"),
    }


@router.delete("/token")
async def clear_cloudflare_token(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Forget the Cloudflare credentials. Does NOT delete the tunnel — to do
    that, call /disconnect first."""
    await cf.clear_state(session)
    return {"ok": True}


@router.get("/zones")
async def list_zones(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    state = await cf.load_state(session)
    token = cf.get_token(state)
    if not token:
        raise HTTPException(400, "Connect a Cloudflare API token first.")
    try:
        zones = await cf.list_zones(token, account_id=state.get("account_id"))
    except cf.CloudflareError as e:
        raise HTTPException(400, f"Cloudflare: {e}")
    return [
        {
            "id": z.get("id"),
            "name": z.get("name"),
            "status": z.get("status"),
            "account_id": (z.get("account") or {}).get("id"),
        }
        for z in zones
    ]


# ───── Tunnel lifecycle (remotely-managed) ────────────────────────────────────


class ConnectTunnelBody(BaseModel):
    name: str = "homebox"
    account_id: str | None = None


async def _adopt_tunnel(
    *,
    token: str,
    account_id: str,
    tunnel: dict[str, Any],
    session: AsyncSession,
) -> dict[str, Any]:
    """Wire an existing Cloudflare tunnel into this admin: persist its id,
    fetch a connector token, push ingress, run cloudflared. Used both when
    we recognize one of our own tunnels on a name collision and when the
    user explicitly accepts adopting an unknown one."""
    tunnel_id = tunnel.get("id")
    if not tunnel_id:
        raise HTTPException(500, "Tunnel object had no id.")
    try:
        connector_token = await cf.get_connector_token_for(token, account_id, tunnel_id)
    except cf.CloudflareError as e:
        raise HTTPException(400, f"Cloudflare: couldn't fetch connector token: {e}")

    state = await cf.load_state(session)
    state["account_id"] = account_id
    state["tunnel_id"] = tunnel_id
    state["tunnel_name"] = tunnel.get("name") or "homebox"
    cf.store_connector_token(state, connector_token)
    await cf.save_state(session, state)

    try:
        await _push_ingress(state, session)
    except cf.CloudflareError as e:
        raise HTTPException(500, f"Tunnel adopted but ingress push failed: {e}")

    ok, msg = run_cloudflared_remote(connector_token)
    if not ok:
        raise HTTPException(500, f"Tunnel adopted but cloudflared failed to start: {msg}")
    return {"ok": True, "tunnel_id": tunnel_id, "tunnel_name": state["tunnel_name"]}


def _auth_error_hint(msg: str) -> str:
    return (
        f"Cloudflare rejected the tunnel-create call: {msg}. "
        "Most likely the API token is missing the "
        "'Account · Cloudflare Tunnel · Edit' permission, or it's "
        "restricted to a different account than the one selected. "
        "Re-create the token from the pre-filled link on the Connect "
        "Cloudflare modal and make sure 'All accounts' (or this "
        "specific account) is in its allowed list."
    )


@router.post("/connect")
async def connect_tunnel(
    body: ConnectTunnelBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    state = await cf.load_state(session)
    token = cf.get_token(state)
    if not token:
        raise HTTPException(400, "Connect a Cloudflare API token first.")

    account_id = body.account_id or state.get("account_id")
    if not account_id:
        raise HTTPException(400, "Pick a Cloudflare account first.")

    name = body.name.strip() or "homebox"
    install_id = await _get_install_id(session)
    metadata = _tunnel_metadata(install_id)

    try:
        tunnel = await cf.create_tunnel(token, account_id, name, metadata=metadata)
    except cf.CloudflareError as create_err:
        # Two recovery paths to try before surfacing the error to the user:
        #   1. Name collision — a tunnel with this name already exists. If
        #      it's tagged as ours (homebox_install_id matches) we adopt
        #      silently. Otherwise we surface a 409 with enough info for
        #      the wizard to ask the user.
        #   2. Auth error — the token is missing scopes or is restricted.
        try:
            existing = await cf.list_tunnels(token, account_id, name=name)
        except cf.CloudflareError:
            existing = []

        if existing:
            match = existing[0]
            if _is_ours(match, install_id):
                result = await _adopt_tunnel(
                    token=token, account_id=account_id, tunnel=match, session=session,
                )
                return {**result, "adopted": True, "ours": True}
            # Not ours — kick the decision back to the user.
            raise HTTPException(
                409,
                detail={
                    "kind": "name_collision",
                    "tunnel": {
                        "id": match.get("id"),
                        "name": match.get("name"),
                        "created_at": match.get("created_at"),
                        "config_src": match.get("config_src"),
                        "connector_count": _connector_count(match),
                        "is_ours": False,
                    },
                    "message": (
                        f"A tunnel named {name!r} already exists in this Cloudflare "
                        "account, and it doesn't look like one Homebox created. "
                        "Adopt it (replace the running connector with this admin's) "
                        "or pick a different name."
                    ),
                },
            )

        # Not a collision — likely auth.
        msg = str(create_err)
        if "auth" in msg.lower() or create_err.status in (401, 403):
            raise HTTPException(400, _auth_error_hint(msg))
        raise HTTPException(400, f"Cloudflare: {msg}")

    tunnel_id = tunnel.get("id")
    if not tunnel_id:
        raise HTTPException(500, "Cloudflare did not return a tunnel id.")
    try:
        connector_token = await cf.get_connector_token_for(token, account_id, tunnel_id)
    except cf.CloudflareError as e:
        raise HTTPException(400, f"Cloudflare: {e}")

    state["account_id"] = account_id
    state["tunnel_id"] = tunnel_id
    state["tunnel_name"] = tunnel.get("name") or name
    cf.store_connector_token(state, connector_token)
    await cf.save_state(session, state)

    try:
        await _push_ingress(state, session)
    except cf.CloudflareError as e:
        raise HTTPException(500, f"Tunnel created but ingress push failed: {e}")

    ok, msg = run_cloudflared_remote(connector_token)
    if not ok:
        raise HTTPException(500, f"Tunnel created but cloudflared failed to start: {msg}")

    return {"ok": True, "tunnel_id": tunnel_id, "tunnel_name": state["tunnel_name"], "adopted": False}


class AdoptTunnelBody(BaseModel):
    tunnel_id: str
    account_id: str | None = None


@router.post("/adopt")
async def adopt_tunnel(
    body: AdoptTunnelBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Explicitly adopt a tunnel by id — used by the wizard after the user
    confirms they want to take over a same-named tunnel they didn't create."""
    state = await cf.load_state(session)
    token = cf.get_token(state)
    if not token:
        raise HTTPException(400, "Connect a Cloudflare API token first.")
    account_id = body.account_id or state.get("account_id")
    if not account_id:
        raise HTTPException(400, "Pick a Cloudflare account first.")

    try:
        tunnel = await cf.get_tunnel(token, account_id, body.tunnel_id)
    except cf.CloudflareError as e:
        raise HTTPException(400, f"Cloudflare: {e}")

    result = await _adopt_tunnel(
        token=token, account_id=account_id, tunnel=tunnel, session=session,
    )
    return {**result, "adopted": True}


@router.post("/disconnect")
async def disconnect_tunnel(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    state = await cf.load_state(session)
    token = cf.get_token(state)
    tunnel_id = state.get("tunnel_id")
    account_id = state.get("account_id")

    remove_container("homebox-cloudflared")

    if token and tunnel_id and account_id:
        try:
            await cf.delete_tunnel(token, account_id, tunnel_id)
        except cf.CloudflareError as e:
            # Don't block the local cleanup on Cloudflare-side errors —
            # surface them so the user can clean up manually if needed.
            raise HTTPException(
                500,
                f"Stopped local connector but Cloudflare delete failed: {e}. "
                "You may need to delete the tunnel manually in the Cloudflare dashboard.",
            )

    state.pop("tunnel_id", None)
    state.pop("tunnel_name", None)
    state.pop("connector_token_encrypted", None)
    await cf.save_state(session, state)

    return {"ok": True}


# ───── Ingress / restart ──────────────────────────────────────────────────────


@router.post("/apply")
async def apply_tunnel(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Push the current Domain rows as the tunnel's ingress via the Cloudflare
    configurations API. Cloudflared picks up the change live — no restart."""
    state = await cf.load_state(session)
    if not state.get("tunnel_id"):
        raise HTTPException(400, "No tunnel configured. Connect a tunnel first.")
    try:
        await _push_ingress(state, session)
    except cf.CloudflareError as e:
        raise HTTPException(500, f"Cloudflare: {e}")
    return {"ok": True}


@router.post("/restart")
async def restart_tunnel(user: str = Depends(require_session_api)):
    ok, msg = restart_container("homebox-cloudflared")
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True}
