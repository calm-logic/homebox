from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import cloudflare as cf
from ..auth import require_session_api
from ..db import get_session
from ..models import Domain
from ..host import write_domains

router = APIRouter(prefix="/api/domains")


class AddDomainBody(BaseModel):
    name: str
    mode: str = "wildcard"
    primary: bool = False


def _serialize(d: Domain) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "mode": d.mode,
        "is_primary": d.is_primary,
        "cloudflare_routed": d.cloudflare_routed,
    }


async def _sync_to_disk(session: AsyncSession) -> None:
    """Persist the canonical domains list. The tunnel's ingress is pushed
    through the Cloudflare API (`_push_remote_ingress`), not via an on-disk
    cloudflared config — there is no local config any more."""
    rows = (await session.execute(select(Domain).order_by(Domain.is_primary.desc(), Domain.name))).scalars().all()
    write_domains([
        {"name": d.name, "mode": d.mode, "primary": d.is_primary}
        for d in rows
    ])


async def _push_remote_ingress(session: AsyncSession) -> None:
    state = await cf.load_state(session)
    token = cf.get_token(state)
    if not token or not state.get("account_id") or not state.get("tunnel_id"):
        return
    rows = (await session.execute(select(Domain).order_by(Domain.name))).scalars().all()
    ingress = cf.build_ingress([{"name": d.name} for d in rows])
    await cf.put_tunnel_config(token, state["account_id"], state["tunnel_id"], ingress)


@router.get("")
async def list_domains(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(select(Domain).order_by(Domain.is_primary.desc(), Domain.name))).scalars().all()
    return [_serialize(d) for d in rows]


@router.post("")
async def add_domain(
    body: AddDomainBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    name = body.name.strip().lower().strip(".")
    if not name:
        raise HTTPException(400, "Domain name is required")
    if body.mode not in ("wildcard", "dedicated"):
        raise HTTPException(400, "Mode must be wildcard or dedicated")

    if body.primary:
        for d in (await session.execute(select(Domain))).scalars():
            d.is_primary = False

    existing = (await session.execute(select(Domain).where(Domain.name == name))).scalar_one_or_none()
    if existing:
        existing.mode = body.mode
        if body.primary:
            existing.is_primary = True
        result = existing
    else:
        result = Domain(
            name=name, mode=body.mode,
            is_primary=body.primary,
        )
        session.add(result)
    await session.commit()
    await session.refresh(result)
    await _sync_to_disk(session)
    try:
        await _push_remote_ingress(session)
    except cf.CloudflareError:
        pass  # ingress will catch up on next /tunnel/apply
    return _serialize(result)


@router.delete("/{domain_id}")
async def delete_domain(
    domain_id: int,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    d = await session.get(Domain, domain_id)
    if not d:
        raise HTTPException(404, "Domain not found")
    await session.delete(d)
    await session.commit()
    await _sync_to_disk(session)
    try:
        await _push_remote_ingress(session)
    except cf.CloudflareError:
        pass  # ingress will catch up on next /tunnel/apply
    return {"ok": True}


# ───── Cloudflare-backed connect (zone picker) ─────────────────────────────────


class ConnectCloudflareBody(BaseModel):
    zone_id: str
    mode: str = "wildcard"
    primary: bool = False


@router.post("/connect-cloudflare")
async def connect_cloudflare_domain(
    body: ConnectCloudflareBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Pick a Cloudflare zone and route it through the active tunnel:
    creates apex + wildcard CNAMEs to <tunnel>.cfargotunnel.com, inserts a
    Domain row, and pushes updated ingress to Cloudflare."""
    if body.mode not in ("wildcard", "dedicated"):
        raise HTTPException(400, "Mode must be wildcard or dedicated")

    state = await cf.load_state(session)
    token = cf.get_token(state)
    if not token:
        raise HTTPException(400, "Connect a Cloudflare API token first.")
    tunnel_id = state.get("tunnel_id")
    if not tunnel_id:
        raise HTTPException(400, "Connect a tunnel first (Tunnel page).")

    try:
        zones = await cf.list_zones(token, account_id=state.get("account_id"))
    except cf.CloudflareError as e:
        raise HTTPException(400, f"Cloudflare: {e}")

    zone = next((z for z in zones if z.get("id") == body.zone_id), None)
    if not zone:
        raise HTTPException(404, "Zone not found in your Cloudflare account.")

    name = (zone.get("name") or "").strip().lower().strip(".")
    if not name:
        raise HTTPException(500, "Cloudflare returned an unnamed zone.")

    target = cf.tunnel_target(tunnel_id)
    try:
        await cf.upsert_cname(token, body.zone_id, name, target, proxied=True)
        if body.mode == "wildcard":
            await cf.upsert_cname(token, body.zone_id, f"*.{name}", target, proxied=True)
    except cf.CloudflareError as e:
        raise HTTPException(400, f"DNS update failed: {e}")

    if body.primary:
        for d in (await session.execute(select(Domain))).scalars():
            d.is_primary = False

    existing = (
        await session.execute(select(Domain).where(Domain.name == name))
    ).scalar_one_or_none()
    if existing:
        existing.mode = body.mode
        existing.cloudflare_routed = True
        if body.primary:
            existing.is_primary = True
        result = existing
    else:
        result = Domain(
            name=name, mode=body.mode,
            is_primary=body.primary,
            cloudflare_routed=True,
        )
        session.add(result)
    await session.commit()
    await session.refresh(result)
    await _sync_to_disk(session)

    try:
        await _push_remote_ingress(session)
    except cf.CloudflareError as e:
        raise HTTPException(500, f"Domain saved but ingress push failed: {e}")

    return _serialize(result)
