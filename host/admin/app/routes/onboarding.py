"""First-run onboarding API.

The admin ships unconfigured: localhost-only, no Cloudflare account, no public
URL. The frontend wizard at /onboarding walks the user through three steps,
each backed by an existing endpoint plus this state probe:

    1. Connect Cloudflare API token  →  POST /api/tunnel/token
    2. Create a Homebox tunnel       →  POST /api/tunnel/connect
    3. (Optional) Pick admin domain  →  POST /api/onboarding/admin-domain

The /state endpoint is what the SPA polls to know whether onboarding is done
and whether to redirect into the wizard.
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import cloudflare as cf
from ..auth import require_session_api
from ..db import get_session
from ..host import write_traefik_dynamic
from ..models import Domain, Project, Setting
from ..webhooks_lib import sync_project_webhook

router = APIRouter(prefix="/api/onboarding")

ADMIN_DOMAIN_KEY = "admin_domain"


# ───── Settings helpers ───────────────────────────────────────────────────────

async def _get_setting(session: AsyncSession, key: str) -> Any:
    row = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    return row.value if row else None


async def _set_setting(session: AsyncSession, key: str, value: Any) -> None:
    row = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    await session.commit()


# ───── State probe ────────────────────────────────────────────────────────────

@router.get("/state")
async def onboarding_state(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Return the per-step status. The SPA gates other routes on `complete`."""
    state = await cf.load_state(session)
    token_set = bool(cf.get_token(state))
    tunnel_set = bool(state.get("tunnel_id"))
    admin_domain = await _get_setting(session, ADMIN_DOMAIN_KEY)

    return {
        "complete": token_set and tunnel_set,
        "steps": {
            "cloudflare_token": {"done": token_set, "account_name": state.get("account_name")},
            "tunnel": {"done": tunnel_set, "tunnel_name": state.get("tunnel_name")},
            "admin_domain": {"done": bool(admin_domain), "hostname": admin_domain},
        },
    }


# ───── Pick admin domain ──────────────────────────────────────────────────────

class AdminDomainBody(BaseModel):
    zone_id: str
    subdomain: str = "admin"  # final host is f"{subdomain}.{zone_name}"; pass "" for the apex


@router.post("/admin-domain")
async def set_admin_domain(
    body: AdminDomainBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Wire up <subdomain>.<zone> as the admin's public URL:
        - Create CNAME → tunnel target
        - Insert Domain row (mode=dedicated — it's admin infra, not a project)
        - Push tunnel ingress so the new host is recognized
        - Write Traefik dynamic_conf.yml with a Host(<admin>) → homebox-admin route
        - Persist admin_domain Setting (the wizard reads this back via /state)
    """
    state = await cf.load_state(session)
    token = cf.get_token(state)
    account_id = state.get("account_id")
    tunnel_id = state.get("tunnel_id")
    if not token:
        raise HTTPException(400, "Connect a Cloudflare token first.")
    if not tunnel_id:
        raise HTTPException(400, "Create a tunnel first.")

    # Validate the zone is in the connected account.
    try:
        zones = await cf.list_zones(token, account_id=account_id)
    except cf.CloudflareError as e:
        raise HTTPException(400, f"Cloudflare: {e}")
    zone = next((z for z in zones if z.get("id") == body.zone_id), None)
    if not zone:
        raise HTTPException(404, "Zone not found in your Cloudflare account.")

    zone_name = (zone.get("name") or "").strip().lower().strip(".")
    sub = (body.subdomain or "").strip().lower().strip(".")
    hostname = f"{sub}.{zone_name}" if sub else zone_name
    # Sanity: hostname must end with the zone name. Defensive — guards against
    # weird subdomains like "x.other.com" sneaking past the zone check.
    if not (hostname == zone_name or hostname.endswith("." + zone_name)):
        raise HTTPException(400, f"Hostname '{hostname}' is not under zone '{zone_name}'.")

    target = cf.tunnel_target(tunnel_id)

    # 1. DNS record
    try:
        await cf.upsert_cname(token, body.zone_id, hostname, target, proxied=True)
    except cf.CloudflareError as e:
        raise HTTPException(400, f"DNS update failed: {e}")

    # 2. DB row (idempotent — replace any existing row for this hostname)
    existing = (
        await session.execute(select(Domain).where(Domain.name == hostname))
    ).scalar_one_or_none()
    if existing:
        existing.mode = "dedicated"
        existing.cloudflare_routed = True
    else:
        session.add(Domain(
            name=hostname, mode="dedicated",
            is_primary=False, cloudflare_routed=True,
        ))
    await session.commit()

    # 3. Push tunnel ingress for ALL domains so the new admin host is in the list.
    rows = (await session.execute(select(Domain).order_by(Domain.name))).scalars().all()
    ingress = cf.build_ingress([{"name": d.name} for d in rows])
    try:
        await cf.put_tunnel_config(token, account_id, tunnel_id, ingress)
    except cf.CloudflareError as e:
        raise HTTPException(500, f"Domain stored but tunnel ingress push failed: {e}")

    # 4. Traefik file-provider route for the admin. Other project domains route
    # through the docker provider (per-project labels), so we only emit the
    # admin route here — that's the one configure.sh used to write at install
    # time and we're now generating dynamically.
    write_traefik_dynamic([
        {
            "name": "homebox-admin",
            "host": hostname,
            "service_url": "http://homebox-admin:8000",
        },
    ])

    # 5. Setting (read back by /state and used as a UI hint).
    await _set_setting(session, ADMIN_DOMAIN_KEY, hostname)

    # 6. The webhook URL just became known (or changed) — re-register push
    # webhooks for projects adopted before this point. Best-effort by design.
    managed = (await session.execute(
        select(Project).where(Project.managed.is_(True))
    )).scalars().all()
    for p in managed:
        await sync_project_webhook(session, p)

    return {"ok": True, "hostname": hostname, "url": f"https://{hostname}"}
