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
        "zone_status": d.zone_status,
        "name_servers": d.name_servers or [],
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


# ───── Unified Cloudflare add: connect existing zone OR create it ─────────────


class CloudflareAddBody(BaseModel):
    name: str
    mode: str = "wildcard"
    primary: bool = False


async def _upsert_domain_row(
    session: AsyncSession, name: str, mode: str, primary: bool, **fields
) -> Domain:
    if primary:
        for d in (await session.execute(select(Domain))).scalars():
            d.is_primary = False
    existing = (await session.execute(select(Domain).where(Domain.name == name))).scalar_one_or_none()
    if existing:
        existing.mode = mode
        if primary:
            existing.is_primary = True
        for k, v in fields.items():
            setattr(existing, k, v)
        result = existing
    else:
        result = Domain(name=name, mode=mode, is_primary=primary, **fields)
        session.add(result)
    await session.commit()
    await session.refresh(result)
    return result


@router.post("/cloudflare")
async def add_cloudflare_domain(
    body: CloudflareAddBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Type any domain name. If it's already a zone in the connected Cloudflare
    account we wire it up immediately (DNS + ingress). If not, we CREATE the
    zone and return the nameservers to set at the registrar — the background
    monitor finishes the wiring once the zone goes active."""
    name = body.name.strip().lower().strip(".")
    if not name or "." not in name:
        raise HTTPException(400, "Enter a full domain name, e.g. example.com")
    if body.mode not in ("wildcard", "dedicated"):
        raise HTTPException(400, "Mode must be wildcard or dedicated")

    state = await cf.load_state(session)
    token = cf.get_token(state)
    account_id = state.get("account_id")
    tunnel_id = state.get("tunnel_id")
    if not token or not account_id:
        raise HTTPException(400, "Connect Cloudflare first (Integrations).")

    try:
        zones = await cf.list_zones(token, account_id=account_id)
    except cf.CloudflareError as e:
        raise HTTPException(400, f"Cloudflare: {e}")
    zone = next((z for z in zones if (z.get("name") or "").lower() == name), None)

    if zone and (zone.get("status") or "").lower() == "active":
        # Existing active zone → same path as the old zone picker.
        if not tunnel_id:
            raise HTTPException(400, "Connect a tunnel first (Integrations → Cloudflare).")
        target = cf.tunnel_target(tunnel_id)
        try:
            await cf.upsert_cname(token, zone["id"], name, target, proxied=True)
            # Wildcard record in BOTH modes: dedicated domains still serve
            # env subdomains (dev.<domain>).
            await cf.upsert_cname(token, zone["id"], f"*.{name}", target, proxied=True)
        except cf.CloudflareError as e:
            raise HTTPException(400, f"DNS update failed: {e}")
        result = await _upsert_domain_row(
            session, name, body.mode, body.primary,
            cloudflare_routed=True, zone_status="active",
            zone_id=zone["id"], name_servers=zone.get("name_servers") or [],
        )
        await _sync_to_disk(session)
        try:
            await _push_remote_ingress(session)
        except cf.CloudflareError as e:
            raise HTTPException(500, f"Domain saved but ingress push failed: {e}")
        return {**_serialize(result), "pending": False}

    if zone:
        # Zone exists but NS delegation hasn't landed yet — track it as pending.
        result = await _upsert_domain_row(
            session, name, body.mode, body.primary,
            cloudflare_routed=False, zone_status="pending",
            zone_id=zone["id"], name_servers=zone.get("name_servers") or [],
        )
        return {**_serialize(result), "pending": True}

    # Brand-new domain — create the zone in Cloudflare.
    try:
        created = await cf.create_zone(token, account_id, name)
    except cf.CloudflareError as e:
        if e.status in (403, 400):
            raise HTTPException(
                403,
                "Cloudflare refused to create the zone — your API token likely "
                "lacks Zone:Edit. Re-issue the token with Zone:Edit and replace "
                "it under Integrations → Cloudflare. "
                f"(Cloudflare said: {e})",
            )
        raise HTTPException(400, f"Zone creation failed: {e}")

    result = await _upsert_domain_row(
        session, name, body.mode, body.primary,
        cloudflare_routed=False, zone_status="pending",
        zone_id=created.get("id"), name_servers=created.get("name_servers") or [],
    )
    await _sync_to_disk(session)
    return {**_serialize(result), "pending": True}


class PatchDomainBody(BaseModel):
    mode: str | None = None       # wildcard | dedicated
    primary: bool | None = None


@router.patch("/{domain_id}")
async def patch_domain(
    domain_id: int,
    body: PatchDomainBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Edit a domain's mode/primary. A mode change on a Cloudflare-routed
    domain also adds (wildcard) or removes (dedicated) the *.domain CNAME."""
    d = await session.get(Domain, domain_id)
    if not d:
        raise HTTPException(404, "Domain not found")
    if body.mode is not None and body.mode not in ("wildcard", "dedicated"):
        raise HTTPException(400, "Mode must be wildcard or dedicated")

    mode_changed = body.mode is not None and body.mode != d.mode
    if body.mode is not None:
        d.mode = body.mode
    if body.primary is not None:
        if body.primary:
            for other in (await session.execute(select(Domain))).scalars():
                other.is_primary = False
            d.is_primary = True
        else:
            d.is_primary = False
    await session.commit()
    await session.refresh(d)
    await _sync_to_disk(session)

    # Both modes keep apex + wildcard records (dedicated still serves env
    # subdomains like dev.<domain>) — just make sure the wildcard exists for
    # legacy dedicated domains that were connected without one.
    if mode_changed and d.cloudflare_routed:
        state = await cf.load_state(session)
        token = cf.get_token(state)
        tunnel_id = state.get("tunnel_id")
        if token and tunnel_id:
            try:
                zone_id = d.zone_id
                if not zone_id:
                    zones = await cf.list_zones(token, account_id=state.get("account_id"))
                    zone = cf.resolve_zone_for(zones, d.name)
                    zone_id = zone["id"] if zone else None
                if zone_id:
                    await cf.upsert_cname(token, zone_id, f"*.{d.name}", cf.tunnel_target(tunnel_id), proxied=True)
            except cf.CloudflareError:
                pass  # hourly DNS check will reconcile

    try:
        await _push_remote_ingress(session)
    except cf.CloudflareError:
        pass
    return _serialize(d)
