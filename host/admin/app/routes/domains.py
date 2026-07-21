from datetime import datetime

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


def _stamp(*rows: Domain) -> None:
    """Mark rows as user-edited NOW. Cluster sync resolves conflicts
    newer-wins on updated_at, so every user-driven domain mutation (including
    is_primary flips on sibling rows) must pass through here."""
    now = datetime.utcnow()
    for r in rows:
        r.updated_at = now


class AddDomainBody(BaseModel):
    name: str
    primary: bool = False


def _serialize(d: Domain) -> dict:
    return {
        "id": d.id,
        "name": d.name,
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
        {"name": d.name, "primary": d.is_primary}
        for d in rows
    ])


async def _push_remote_ingress(session: AsyncSession) -> None:
    state = await cf.load_state(session)
    token = cf.get_token(state)
    if not token or not state.get("account_id") or not state.get("tunnel_id"):
        return
    rows = (await session.execute(select(Domain).order_by(Domain.name))).scalars().all()
    from ..targetslib import all_tunnel_tcp_rules
    ingress = cf.build_ingress(
        [{"name": d.name} for d in rows],
        tcp_rules=await all_tunnel_tcp_rules(session),
    )
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

    if body.primary:
        for d in (await session.execute(select(Domain))).scalars():
            d.is_primary = False
            _stamp(d)

    existing = (await session.execute(select(Domain).where(Domain.name == name))).scalar_one_or_none()
    if existing:
        if body.primary:
            existing.is_primary = True
        result = existing
    else:
        result = Domain(
            name=name,
            is_primary=body.primary,
        )
        session.add(result)
    _stamp(result)
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
    from .. import cluster_sync
    await cluster_sync.record_tombstone(session, "domain", d.name, commit=False)
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
            _stamp(d)

    existing = (
        await session.execute(select(Domain).where(Domain.name == name))
    ).scalar_one_or_none()
    if existing:
        existing.cloudflare_routed = True
        if body.primary:
            existing.is_primary = True
        result = existing
    else:
        result = Domain(
            name=name,
            is_primary=body.primary,
            cloudflare_routed=True,
        )
        session.add(result)
    _stamp(result)
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
    primary: bool = False


async def _upsert_domain_row(
    session: AsyncSession, name: str, primary: bool, **fields
) -> Domain:
    if primary:
        for d in (await session.execute(select(Domain))).scalars():
            d.is_primary = False
            _stamp(d)
    existing = (await session.execute(select(Domain).where(Domain.name == name))).scalar_one_or_none()
    if existing:
        if primary:
            existing.is_primary = True
        for k, v in fields.items():
            setattr(existing, k, v)
        result = existing
    else:
        result = Domain(name=name, is_primary=primary, **fields)
        session.add(result)
    _stamp(result)
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
            # A wildcard record is always created — some project assigned to
            # this domain later might use base mode, which still serves
            # env subdomains (dev.<domain>).
            await cf.upsert_cname(token, zone["id"], f"*.{name}", target, proxied=True)
        except cf.CloudflareError as e:
            raise HTTPException(400, f"DNS update failed: {e}")
        result = await _upsert_domain_row(
            session, name, body.primary,
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
            session, name, body.primary,
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
        session, name, body.primary,
        cloudflare_routed=False, zone_status="pending",
        zone_id=created.get("id"), name_servers=created.get("name_servers") or [],
    )
    await _sync_to_disk(session)
    return {**_serialize(result), "pending": True}


# ───── Domain usage drilldown ─────────────────────────────────────────────────


_TARGET_LABELS = {"aws": "AWS", "gcp": "Google Cloud", "cloudflare": "Cloudflare"}


def _location_for(resolved, locations: list[dict]) -> dict:
    """Where a resolved service target runs, as {kind, id, name} for the UI.
    Cloud targets name the provider; homebox targets name the cluster/node
    from the account overview (falling back to the raw id), or the local
    install when no location is pinned."""
    if resolved.target != "homebox":
        return {"kind": "cloud", "id": resolved.target,
                "name": _TARGET_LABELS.get(resolved.target, resolved.target)}
    loc = resolved.location or {}
    if loc.get("cluster_id"):
        name = next((l["name"] for l in locations
                     if l["kind"] == "cluster" and l["id"] == loc["cluster_id"]),
                    loc["cluster_id"])
        return {"kind": "cluster", "id": loc["cluster_id"], "name": name}
    if loc.get("node_id"):
        name = next((l["name"] for l in locations
                     if l["kind"] == "node" and l["id"] == loc["node_id"]),
                    loc["node_id"])
        return {"kind": "node", "id": loc["node_id"], "name": name}
    local = next((l for l in locations if l.get("local")), None)
    return {"kind": "local", "id": local["id"] if local else None,
            "name": local["name"] if local else "This Homebox"}


@router.get("/{domain_id}")
async def domain_usage(
    domain_id: int,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """The domain plus everything served under it: one connection per
    (public service, environment) whose effective domain resolves here
    (env override → project domain → primary fallback), with the canonical
    hostname derived exactly like deploys derive it (app/urls.py), where the
    service runs (local/cluster/node/cloud) and its latest instance status.
    Also lists per-host DNS override records this install wrote on the
    domain (targetslib.load_dns_overrides)."""
    from .. import targetslib, urls
    from ..models import Deployment, Environment, Project, Service, ServiceInstance
    from .services import _account_locations

    d = await session.get(Domain, domain_id)
    if not d:
        raise HTTPException(404, "Domain not found")

    primary = (await session.execute(
        select(Domain).where(Domain.is_primary == True)  # noqa: E712
    )).scalars().first()
    locations, _linked = await _account_locations(session)

    projects = (await session.execute(
        select(Project).where(Project.managed == True)  # noqa: E712
    )).scalars().all()

    connections: list[dict] = []
    for project in projects:
        base = project.domain_mode == "base"
        envs = (await session.execute(
            select(Environment).where(Environment.project_id == project.id)
        )).scalars().all()
        public = [s for s in (await session.execute(
            select(Service).where(Service.project_id == project.id)
        )).scalars().all() if s.is_public]
        if not public:
            continue
        for env in envs:
            effective = env.domain_id or project.domain_id \
                or (primary.id if primary else None)
            if effective != d.id:
                continue
            targets_map = await targetslib.effective_targets(session, project, env)
            latest = (await session.execute(
                select(Deployment).where(Deployment.environment_id == env.id)
                .order_by(Deployment.created_at.desc()).limit(1)
            )).scalars().first()
            instances: dict[str, ServiceInstance] = {}
            if latest:
                instances = {i.service_name: i for i in (await session.execute(
                    select(ServiceInstance)
                    .where(ServiceInstance.deployment_id == latest.id)
                )).scalars().all()}
            for svc in public:
                label = svc.subdomain_label or ""
                host = urls.full_host(
                    project.name, "" if base else label,
                    env.slug_suffix or "", d.name, base=base)
                path = f"/{label}" if base and label else None
                resolved = targetslib.resolve_for(targets_map, svc.name)
                inst = instances.get(svc.name)
                status = (resolved.state.get("status") if resolved.cloud
                          else (inst.status if inst else None))
                connections.append({
                    "hostname": host,
                    "path": path,
                    "url": f"https://{host}{path or ''}",
                    "project_id": project.id,
                    "project_name": project.name,
                    "project_icon": project.icon,
                    "environment_id": env.id,
                    "environment_name": env.name,
                    "service_id": svc.id,
                    "service_name": svc.name,
                    "service_kind": svc.kind,
                    "target": resolved.target,
                    "location": _location_for(resolved, locations),
                    "status": status,
                    "deploy_status": latest.status if latest else None,
                })

    connections.sort(key=lambda c: (c["hostname"], c["path"] or ""))

    overrides = await targetslib.load_dns_overrides(session)
    suffix = f".{d.name.lower()}"
    own_overrides = [
        {"hostname": host, **{k: meta.get(k) for k in
         ("cname_target", "project", "env", "service", "created_at")}}
        for host, meta in sorted(overrides.items())
        if (meta.get("domain") or "").lower() == d.name.lower()
        or host.lower().endswith(suffix)
    ]

    return {**_serialize(d), "connections": connections,
            "dns_overrides": own_overrides}


class PatchDomainBody(BaseModel):
    primary: bool | None = None


@router.patch("/{domain_id}")
async def patch_domain(
    domain_id: int,
    body: PatchDomainBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Set a domain as primary (the fallback for projects with no domain
    assigned)."""
    d = await session.get(Domain, domain_id)
    if not d:
        raise HTTPException(404, "Domain not found")

    if body.primary is not None:
        if body.primary:
            for other in (await session.execute(select(Domain))).scalars():
                other.is_primary = False
                _stamp(other)
            d.is_primary = True
        else:
            d.is_primary = False
        _stamp(d)
    await session.commit()
    await session.refresh(d)
    await _sync_to_disk(session)

    try:
        await _push_remote_ingress(session)
    except cf.CloudflareError:
        pass
    return _serialize(d)
