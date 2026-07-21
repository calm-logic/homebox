"""First-run onboarding API.

The admin ships unconfigured: localhost-only, no Cloudflare account, no public
URL. The frontend wizard at /onboarding walks the user through three steps,
each backed by an existing endpoint plus this state probe:

    1. Connect Cloudflare API token  →  POST /api/tunnel/token
    2. Create a Homebox tunnel       →  POST /api/tunnel/connect
    3. (Optional) Pick admin domain  →  POST /api/onboarding/admin-domain

The fast path ("Log in with Homebox", demo-video gap G7): linking a homebox.sh
account restores the account vault, which imports the Integration rows —
including the Cloudflare one with its encrypted token. Step 1 is therefore
already satisfied without a paste (cf.load_state reads that same Integration
row), /state reports it with a `synced` flag, and POST /auto-tunnel lets the
wizard advance step 2 with a single call.

The /state endpoint is what the SPA polls to know whether onboarding is done
and whether to redirect into the wizard.
"""

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import cloudflare as cf
from ..auth import require_session_api
from ..clusterlib import load_account
from ..db import get_session
from ..host import write_traefik_dynamic
from ..models import Domain, Integration, Project, Setting
from ..vaultlib import get_vault_state
from ..webhooks_lib import sync_project_webhook
from .tunnel import ConnectTunnelBody, connect_tunnel

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

async def _cloudflare_synced(session: AsyncSession, token_set: bool) -> bool:
    """True when the Cloudflare integration arrived via the account-vault
    restore rather than a manual token paste. Inference: the vault recorded a
    pull, and the Integration row hasn't been touched since (a manual POST
    /api/tunnel/token stamps `updated_at = now` > pulled_at, while the vault
    import preserves the exported row's original, older timestamp)."""
    if not token_set:
        return False
    vs = await get_vault_state(session)
    pulled_raw = vs.get("pulled_at")
    if not pulled_raw:
        return False
    try:
        pulled = datetime.fromisoformat(str(pulled_raw))
    except ValueError:
        return False
    row = (await session.execute(
        select(Integration).where(Integration.provider == cf.PROVIDER)
    )).scalar_one_or_none()
    if row is None:
        return False
    return row.updated_at is None or row.updated_at <= pulled


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

    # Account fast path: linked homebox.sh account + vault-restore progress.
    linked = (await load_account(session)) is not None
    vault_state = await get_vault_state(session)

    return {
        "complete": token_set and tunnel_set,
        "account": {
            "linked": linked,
            # True while restore_on_link is importing the vault — the wizard
            # shows "Syncing from your account…" until steps flip done.
            "restoring": bool(vault_state.get("restoring")),
        },
        "steps": {
            "cloudflare_token": {
                "done": token_set,
                "account_name": state.get("account_name"),
                "synced": await _cloudflare_synced(session, token_set),
                # Optional deploy-target capabilities the stored token carries
                # (probed at connect time). Let the wizard show a "Cloudflare
                # Pages / Workers ready" checklist without re-hitting the CF API.
                "pages_ok": bool(state.get("pages_ok")) if token_set else False,
                "workers_ok": bool(state.get("workers_ok")) if token_set else False,
            },
            "tunnel": {"done": tunnel_set, "tunnel_name": state.get("tunnel_name")},
            "admin_domain": {"done": bool(admin_domain), "hostname": admin_domain},
        },
    }


# ───── Auto-tunnel (fast path, step 2 in one call) ────────────────────────────

@router.post("/auto-tunnel")
async def auto_tunnel(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Create (or adopt) this box's tunnel with the default name — the wizard
    calls this once the Cloudflare step auto-completes from a synced
    integration, so step 2 needs no form. Delegates to the same connect flow
    as POST /api/tunnel/connect (name collisions adopt our own tunnel there).
    Idempotent: a tunnel that's already configured is reported as-is."""
    state = await cf.load_state(session)
    if state.get("tunnel_id"):
        return {
            "ok": True,
            "already": True,
            "tunnel_id": state["tunnel_id"],
            "tunnel_name": state.get("tunnel_name"),
        }
    if not cf.get_token(state):
        raise HTTPException(400, "Connect a Cloudflare API token first.")
    return await connect_tunnel(ConnectTunnelBody(), user=user, session=session)


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
        - Insert Domain row (admin infra, not a project)
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
        existing.cloudflare_routed = True
    else:
        session.add(Domain(
            name=hostname,
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
