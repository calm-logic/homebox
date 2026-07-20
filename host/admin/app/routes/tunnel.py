"""Tunnel + Cloudflare credentials API.

Cloudflared runs in remotely-managed mode only: just a connector token, no
config.yml/credentials JSON on disk, ingress rules pushed via the Cloudflare
API. Set up by the admin's onboarding wizard; this module exposes the status
+ lifecycle endpoints the UI calls into.
"""

from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import cloudflare as cf
from ..auth import require_session_api
from ..db import get_session
from ..models import Domain, ServiceInstance, Setting, UptimeSample
from ..host import (
    container_status,
    remove_container,
    restart_container,
    run_cloudflared_remote,
)

router = APIRouter(prefix="/api/tunnel")


async def _get_install_id(session: AsyncSession) -> str:
    """This install's stable random identifier — used as
    `metadata.homebox_install_id` on every tunnel we create so we can recognize
    our own on re-runs. Shared with clusterlib, where it doubles as the
    cluster node id."""
    from ..clusterlib import get_node_id
    return await get_node_id(session)


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
        "id": d.id, "name": d.name,
        "is_primary": d.is_primary,
        "cloudflare_routed": d.cloudflare_routed,
    }


async def _push_ingress(state: dict[str, Any], session: AsyncSession) -> None:
    token = cf.get_token(state)
    if not token or not state.get("account_id") or not state.get("tunnel_id"):
        return
    domains = (
        await session.execute(select(Domain).order_by(Domain.name))
    ).scalars().all()
    from ..targetslib import all_tunnel_tcp_rules, load_dns_overrides
    # Per-host DNS overrides (cross-cluster domain sharing, G12): hostnames we
    # serve under a domain whose wildcard belongs to another cluster's tunnel
    # get explicit http rules. Kwarg passed only when any exist, keeping the
    # call shape stable for the common case.
    overrides = await load_dns_overrides(session)
    extra = {"extra_hostnames": sorted(overrides)} if overrides else {}
    ingress = cf.build_ingress(
        [{"name": d.name} for d in domains],
        tcp_rules=await all_tunnel_tcp_rules(session),
        **extra,
    )
    await cf.put_tunnel_config(token, state["account_id"], state["tunnel_id"], ingress)


# ───── DNS routing health / repair ────────────────────────────────────────────
#
# Ingress tells the tunnel which hostnames to serve; DNS tells Cloudflare's edge
# which tunnel to send a hostname to. The two are pushed independently, and DNS
# CNAMEs are only written when a domain is first routed — pinned to whatever
# tunnel id existed then. Re-create or adopt a tunnel (new id) and every CNAME
# silently points at the old, dead target → Cloudflare Error 1033. These helpers
# detect that drift and repoint the records at the live tunnel.


def _dns_hostnames(d: Domain) -> list[str]:
    """The CNAME record names Homebox manages for a routed domain: apex +
    wildcard, always (a project on base mode still serves env subdomains
    like dev.<domain>). Mirrors cf.build_ingress."""
    return [d.name, f"*.{d.name}"]


async def _all_domains(session: AsyncSession) -> list[Domain]:
    return list(
        (await session.execute(select(Domain).order_by(Domain.name))).scalars().all()
    )


async def _served_hostnames(session: AsyncSession) -> set[str]:
    """Hostnames this install serves through the tunnel — from deployed service
    instance URLs. Used to repair stale per-project DNS records (left by older
    installs/tunnels) that shadow the wildcard and cause 530/1033."""
    urls = (await session.execute(
        select(ServiceInstance.url).where(ServiceInstance.url.is_not(None)).distinct()
    )).scalars().all()
    return {u.split("://", 1)[-1].split("/", 1)[0].lower() for u in urls if u}


async def _foreign_wildcard_target(
    token: str, account_id: str, zone: dict[str, Any], domain_name: str,
    our_target: str, live_cache: dict[str, bool],
) -> str | None:
    """The domain's `*.<domain>` (apex fallback) CNAME target when it points at
    ANOTHER cluster's LIVE tunnel — i.e. the wildcard routing is owned
    elsewhere (linked accounts, G12) and must not be repointed or reported as
    drift here. None when the wildcard is ours, absent, not a tunnel CNAME, or
    aimed at a DEAD tunnel (that's classic drift to repair, not foreign
    ownership). API errors → None: the report/repair proceeds exactly as it
    always has."""
    try:
        content = None
        for name in (f"*.{domain_name}", domain_name):
            recs = await cf.list_dns_records(token, zone["id"], name=name)
            cname = next((r for r in recs if r.get("type") == "CNAME"), None)
            if cname:
                content = (cname.get("content") or "").strip().lower().strip(".")
                break
        if not content or not content.endswith(".cfargotunnel.com") \
                or content == our_target.lower():
            return None
        other_id = content.split(".", 1)[0]
        if other_id not in live_cache:
            try:
                other = await cf.get_tunnel(token, account_id, other_id)
                live_cache[other_id] = bool(other.get("connections"))
            except cf.CloudflareError:
                live_cache[other_id] = False  # uninspectable → dead → repairable
        return content if live_cache[other_id] else None
    except cf.CloudflareError:
        return None


async def _dns_report(state: dict[str, Any], session: AsyncSession) -> dict[str, Any]:
    """Compare every tunnel-served hostname's CNAME against the current tunnel
    target so the UI can surface stale/missing records before they cause an
    Error 1033. Domains whose zone isn't in the connected Cloudflare account are
    reported as `no_zone` (informational — Homebox can't manage their DNS) and
    don't count against `in_sync`."""
    token = cf.get_token(state)
    tunnel_id = state.get("tunnel_id")
    account_id = state.get("account_id")
    report: dict[str, Any] = {
        "checked": False,
        "in_sync": True,
        "tunnel_target": cf.tunnel_target(tunnel_id) if tunnel_id else None,
        "records": [],
    }
    if not token or not tunnel_id or not account_id:
        return report

    target = cf.tunnel_target(tunnel_id)
    try:
        zones = await cf.list_zones(token, account_id=account_id)
    except cf.CloudflareError as e:
        report["error"] = str(e)
        report["in_sync"] = False
        return report

    # Hostnames routed to a CLOUD deployment target (Pages/S3/Cloud Run/…)
    # deliberately do NOT point at the tunnel — their expected value is the
    # target's CNAME, and the repair below must never "fix" them back. Same
    # for hostnames whose service is homebox-targeted at a DIFFERENT
    # cluster/node (linked accounts): the OWNING cluster manages those
    # records (cname_target None keeps them out of the specific-host report
    # rows below — we can't know the foreign tunnel's target).
    from ..targetslib import (
        cloud_routed_hostnames, foreign_homebox_hostnames, load_dns_overrides,
    )
    excluded = await cloud_routed_hostnames(session)
    excluded.update(await foreign_homebox_hostnames(session))
    # Per-host overrides WE wrote on a foreign-owned domain (G12): deliberate
    # specific-beats-wildcard records whose expected target is OUR tunnel —
    # they get their own report rows below, never counted as drift.
    for host in await load_dns_overrides(session):
        excluded.setdefault(host.lower(), {
            "cname_target": target, "proxied": True, "target": "homebox",
        })

    report["checked"] = True
    seen_hosts: set[str] = set()
    live_cache: dict[str, bool] = {}
    for d in await _all_domains(session):
        zone = cf.resolve_zone_for(zones, d.name)
        # Apex/wildcard owned by ANOTHER cluster's live tunnel: informational,
        # not drift — that cluster manages these records.
        foreign_target = await _foreign_wildcard_target(
            token, account_id, zone, d.name, target, live_cache) if zone else None
        for host in _dns_hostnames(d):
            excl = excluded.get(host.lower())
            expected = (excl.get("cname_target") or target) if excl \
                else (foreign_target or target)
            want_proxied = excl.get("proxied", True) if excl else True
            seen_hosts.add(host.lower())
            entry: dict[str, Any] = {
                "hostname": host,
                "domain": d.name,
                "zone": zone.get("name") if zone else None,
                "expected": expected,
                "actual": None,
                "proxied": None,
                "status": "ok",
            }
            if excl:
                entry["cloud_target"] = excl.get("target")
            if not zone:
                # Not on Cloudflare (or different account) — not ours to manage.
                entry["status"] = "no_zone"
            else:
                try:
                    recs = await cf.list_dns_records(token, zone["id"], name=host)
                except cf.CloudflareError as e:
                    entry["status"] = "error"
                    entry["error"] = str(e)
                    recs = None
                if recs is not None:
                    cname = next((r for r in recs if r.get("type") == "CNAME"), None)
                    if not cname:
                        entry["status"] = "missing"
                    else:
                        actual = (cname.get("content") or "").strip().lower().strip(".")
                        proxied = bool(cname.get("proxied"))
                        entry["actual"] = actual
                        entry["proxied"] = proxied
                        entry["status"] = (
                            "ok" if actual == (expected or "").lower()
                            and proxied == want_proxied else "stale"
                        )
            if foreign_target and not excl:
                # Another cluster's records: whatever their state, they're not
                # OURS to fix — surface as informational, like no_zone.
                entry["foreign_cluster"] = True
                if entry["status"] in ("stale", "missing"):
                    entry["status"] = "foreign"
            # `no_zone`/`foreign` are informational, not failures we can fix.
            if entry["status"] not in ("ok", "no_zone", "foreign"):
                report["in_sync"] = False
            report["records"].append(entry)

    # Cloud-routed SPECIFIC hostnames (not the apex/wildcard) get their own
    # report rows so drift on them is visible too.
    for host, excl in excluded.items():
        if host in seen_hosts or not excl.get("cname_target"):
            continue
        zone = cf.resolve_zone_for(zones, host)
        entry = {
            "hostname": host, "domain": None,
            "zone": zone.get("name") if zone else None,
            "expected": excl["cname_target"], "actual": None, "proxied": None,
            "status": "ok", "cloud_target": excl.get("target"),
        }
        if not zone:
            entry["status"] = "no_zone"
        else:
            try:
                recs = await cf.list_dns_records(token, zone["id"], name=host)
                cname = next((r for r in recs if r.get("type") == "CNAME"), None)
                if not cname:
                    entry["status"] = "missing"
                else:
                    actual = (cname.get("content") or "").strip().lower().strip(".")
                    entry["actual"] = actual
                    entry["proxied"] = bool(cname.get("proxied"))
                    entry["status"] = "ok" if actual == excl["cname_target"].lower() else "stale"
            except cf.CloudflareError as e:
                entry["status"] = "error"
                entry["error"] = str(e)
        if entry["status"] not in ("ok", "no_zone"):
            report["in_sync"] = False
        report["records"].append(entry)
    return report


async def _resync_dns(state: dict[str, Any], session: AsyncSession) -> dict[str, Any]:
    """Repoint every tunnel-served hostname's CNAME at the current tunnel
    target. Idempotent and safe to run any time the tunnel id may have changed
    (after connect/adopt, or on demand from the UI). Best-effort per record —
    one zone/record failure doesn't abort the rest. Domains we successfully wire
    are flagged `cloudflare_routed` so status reflects that Homebox owns them."""
    token = cf.get_token(state)
    tunnel_id = state.get("tunnel_id")
    account_id = state.get("account_id")
    result: dict[str, Any] = {
        "updated": [], "skipped": [], "errors": [], "tunnel_target": None,
    }
    if not token or not tunnel_id or not account_id:
        return result

    target = cf.tunnel_target(tunnel_id)
    result["tunnel_target"] = target
    zones = await cf.list_zones(token, account_id=account_id)

    # Hostnames owned by a cloud deployment target must NOT be repointed at
    # the tunnel — that's the exact record the cloud deploy wrote. Missing an
    # exclusion here would silently break the cloud service every hour (the
    # monitor runs this repair on a schedule). Hostnames whose service is
    # homebox-targeted at a DIFFERENT cluster/node are excluded for the same
    # reason: their CNAME points at the OWNING cluster's tunnel, and
    # repointing it here would steal the traffic to a node not running the
    # service.
    from ..targetslib import (
        cloud_routed_hostnames, foreign_homebox_hostnames, load_dns_overrides,
    )
    excluded = await cloud_routed_hostnames(session)
    excluded.update(await foreign_homebox_hostnames(session))
    # Per-host overrides WE wrote on a foreign-owned domain (G12): actively
    # maintained at OUR tunnel below, and excluded from the generic stale-
    # record sweep so it never second-guesses them.
    overrides = await load_dns_overrides(session)
    for host in overrides:
        excluded.setdefault(host.lower(), {
            "cname_target": target, "proxied": True, "target": "homebox",
        })

    dirty = False
    live_cache: dict[str, bool] = {}
    for d in await _all_domains(session):
        zone = cf.resolve_zone_for(zones, d.name)
        if not zone:
            result["skipped"].append(
                {"hostname": d.name, "reason": "no connected Cloudflare zone covers this domain"}
            )
            continue
        # A wildcard pointing at ANOTHER cluster's LIVE tunnel is ownership,
        # not drift — repointing it would steal every app that cluster serves
        # under the domain. (A DEAD foreign tunnel is still repaired: that is
        # the classic re-created-tunnel Error 1033 case.)
        foreign_target = await _foreign_wildcard_target(
            token, account_id, zone, d.name, target, live_cache)
        if foreign_target:
            result["skipped"].append({
                "hostname": f"*.{d.name}",
                "reason": f"wildcard owned by another cluster's live tunnel ({foreign_target})",
            })
            continue
        wired_any = False
        for host in _dns_hostnames(d):
            if host.lower() in excluded:
                result["skipped"].append(
                    {"hostname": host, "reason": "owned by a cloud deployment target"}
                )
                continue
            try:
                await cf.upsert_cname(token, zone["id"], host, target, proxied=True)
                result["updated"].append(host)
                wired_any = True
            except cf.CloudflareError as e:
                result["errors"].append({"hostname": host, "error": str(e)})
        if wired_any and not d.cloudflare_routed:
            d.cloudflare_routed = True
            dirty = True
    if dirty:
        await session.commit()

    # Per-host overrides we own (G12): keep them pinned at OUR tunnel — a
    # drifted or deleted override record breaks exactly the hostname it was
    # created to serve (the wildcard would hand it back to the other cluster).
    for host in sorted(overrides):
        zone = cf.resolve_zone_for(zones, host)
        if not zone:
            result["skipped"].append(
                {"hostname": host, "reason": "no connected Cloudflare zone covers this override"}
            )
            continue
        try:
            recs = await cf.list_dns_records(token, zone["id"], name=host)
            cname = next((r for r in recs if r.get("type") == "CNAME"), None)
            actual = (cname.get("content") or "").strip().lower().strip(".") \
                if cname else None
            if actual == target.lower() and bool(cname.get("proxied")):
                continue  # already correct — nothing to write
            await cf.upsert_cname(token, zone["id"], host, target, proxied=True)
            result["updated"].append(host)
        except cf.CloudflareError as e:
            result["errors"].append({"hostname": host, "error": str(e)})

    # Per-project hostnames: a leftover specific record (older install, old
    # tunnel id) shadows the wildcard and 530s just that host. Repoint any
    # tunnel CNAME for a hostname we serve that targets a different tunnel.
    served = await _served_hostnames(session)
    for z in zones:
        suffix = "." + z["name"]
        hosts_in_zone = {h for h in served if h.endswith(suffix) or h == z["name"]}
        if not hosts_in_zone:
            continue
        try:
            records = await cf.list_dns_records(token, z["id"])
        except cf.CloudflareError as e:
            result["errors"].append({"hostname": f"*{suffix}", "error": str(e)})
            continue
        for r in records:
            name = (r.get("name") or "").lower()
            if name in excluded:
                # Cloud deployment target owns this record (it doesn't contain
                # cfargotunnel.com today, but never rely on that shape).
                continue
            if (r.get("type") == "CNAME" and name in hosts_in_zone
                    and "cfargotunnel.com" in (r.get("content") or "")
                    and r.get("content") != target):
                # Only repoint records aimed at DEAD tunnels. A record whose
                # target tunnel has live connections is deliberate — e.g. a
                # developer's own cloudflared exposing a dev server on a
                # hostname a project once served. Repointing it once broke a
                # `make up` dev flow for days.
                other_id = (r.get("content") or "").split(".", 1)[0]
                try:
                    other = await cf.get_tunnel(token, account_id, other_id)
                    if other.get("connections"):
                        result["skipped"].append({
                            "hostname": name,
                            "reason": f"points at live tunnel {other.get('name') or other_id}",
                        })
                        continue
                except cf.CloudflareError:
                    pass  # can't inspect it (foreign account / deleted) — treat as dead
                try:
                    await cf.upsert_cname(token, z["id"], name, target, proxied=True)
                    result["updated"].append(name)
                except cf.CloudflareError as e:
                    result["errors"].append({"hostname": name, "error": str(e)})
    return result


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


def _missing_scope_hint(missing: list[str]) -> str:
    items = "; ".join(missing)
    return (
        f"This token is missing required Cloudflare permission(s): {items}. "
        "Cloudflare's pre-filled token link doesn't reliably include the Tunnel scope, so add the "
        "missing rows by hand on the Create Token page, set Account Resources to 'All accounts' "
        "(or this account), then paste the new token again."
    )


async def _validate_and_store_token(
    session: AsyncSession, token: str, account_id: str | None = None,
) -> dict[str, Any]:
    """Verify a Cloudflare token, resolve its account, probe the scopes Homebox
    needs, and persist it. Shared by the paste endpoint and the browser-login
    completion (the cert.pem token is a normal account-scoped token). Raises
    HTTPException with a precise message on any problem."""
    token = token.strip()
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

    # Probe the scopes Homebox actually needs BEFORE persisting. verify +
    # list_accounts above pass with read-only scopes, so without this a token
    # missing 'Cloudflare Tunnel: Edit' or 'Zone: Read' is accepted here and only
    # fails later, mid-onboarding. (DNS: Edit can't be read-probed without a
    # write, so it's surfaced in the UI checklist instead.)
    if state.get("account_id"):
        missing: list[str] = []

        def _is_auth(e: cf.CloudflareError) -> bool:
            return e.status in (401, 403) or "auth" in str(e).lower()

        try:
            await cf.list_tunnels(token, state["account_id"])
        except cf.CloudflareError as e:
            if _is_auth(e):
                missing.append("Account · Cloudflare Tunnel · Edit")
        try:
            await cf.list_zones(token, account_id=state["account_id"])
        except cf.CloudflareError as e:
            if _is_auth(e):
                missing.append("Zone · Zone · Read")
        if missing:
            raise HTTPException(400, _missing_scope_hint(missing))

        # Pages scope is OPTIONAL (only the Pages deployment target needs it)
        # — never block the token on it, just record the capability so the
        # targets UI can hint "re-scope your token" up front.
        try:
            await cf.list_pages_projects(token, state["account_id"])
            state["pages_ok"] = True
        except cf.CloudflareError as e:
            state["pages_ok"] = not _is_auth(e)

    await cf.save_state(session, state)

    return {
        "ok": True,
        "accounts": [{"id": a.get("id"), "name": a.get("name")} for a in accounts],
        "account_id": state.get("account_id"),
        "account_name": state.get("account_name"),
    }


@router.post("/token")
async def set_cloudflare_token(
    body: TokenBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    return await _validate_and_store_token(session, body.token, body.account_id)


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

    # Adopting a tunnel changes the tunnel id, so any CNAMEs from a previous
    # tunnel now point at a dead target. Repoint them at this one (best-effort —
    # the DNS health panel surfaces anything that didn't take).
    try:
        await _resync_dns(state, session)
    except cf.CloudflareError:
        pass

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

    # A brand-new tunnel has a fresh id; repoint any already-routed domains'
    # CNAMEs at it so re-running onboarding doesn't strand them on the old
    # tunnel (the Error 1033 trap). Best-effort.
    try:
        await _resync_dns(state, session)
    except cf.CloudflareError:
        pass

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


@router.get("/dns")
async def dns_health(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Per-hostname routing health: does each managed CNAME still point at the
    live tunnel? `stale`/`missing`/`no_zone` records are the Error 1033 cause."""
    state = await cf.load_state(session)
    if not state.get("tunnel_id"):
        raise HTTPException(400, "No tunnel configured. Connect a tunnel first.")
    return await _dns_report(state, session)


@router.get("/dns-status")
async def dns_status(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Last background DNS drift check (monitor runs it hourly). The Domains
    page shows a banner only when this reports problems."""
    row = (await session.execute(
        select(Setting).where(Setting.key == "dns_status")
    )).scalar_one_or_none()
    return row.value if row and isinstance(row.value, dict) else {"checked_at": None, "in_sync": True, "issues": [], "repaired": []}


@router.post("/resync-dns")
async def resync_dns(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Repoint every managed CNAME at the current tunnel target. Fixes routing
    after a tunnel was re-created/adopted and the DNS records went stale."""
    state = await cf.load_state(session)
    if not state.get("tunnel_id"):
        raise HTTPException(400, "No tunnel configured. Connect a tunnel first.")
    try:
        result = await _resync_dns(state, session)
    except cf.CloudflareError as e:
        raise HTTPException(500, f"Cloudflare: {e}")
    # Surface a hard failure only if nothing was fixed and something errored —
    # partial success (some records updated, some zones missing) still returns ok
    # with the per-record detail so the UI can show what's left.
    if result["errors"] and not result["updated"]:
        raise HTTPException(500, f"DNS resync failed: {result['errors']}")
    return {"ok": True, **result}


@router.post("/restart")
async def restart_tunnel(user: str = Depends(require_session_api)):
    ok, msg = restart_container("homebox-cloudflared")
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True}


# ───── Uptime (background monitor) ────────────────────────────────────────────

_UPTIME_WINDOWS = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "14d": timedelta(days=14),
}
# Display order; admin_url (true end-to-end) first.
_UPTIME_COMPONENTS = ("admin_url", "tunnel", "cloudflared", "traefik", "docker_proxy")
_TIMELINE_POINTS = 60  # most recent samples returned per component for a sparkline


@router.get("/uptime")
async def tunnel_uptime(
    window: str = "24h",
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Uptime % + recent status timeline per infrastructure component, computed
    from the UptimeSample rows written by app/monitor.py. 'unknown' samples
    (component not configured yet) are excluded from the percentage."""
    win = window if window in _UPTIME_WINDOWS else "24h"
    since = datetime.utcnow() - _UPTIME_WINDOWS[win]

    rows = (await session.execute(
        select(UptimeSample)
        .where(UptimeSample.ts >= since)
        .order_by(UptimeSample.ts.asc())
    )).scalars().all()

    by_comp: dict[str, list[UptimeSample]] = {}
    for s in rows:
        by_comp.setdefault(s.component, []).append(s)

    components = []
    for comp in _UPTIME_COMPONENTS:
        samples = by_comp.get(comp, [])
        measured = [s for s in samples if s.status != "unknown"]
        up = sum(1 for s in measured if s.status in ("up", "degraded"))
        latest = samples[-1] if samples else None
        timeline = [
            {"ts": s.ts.isoformat(), "status": s.status, "latency_ms": s.latency_ms}
            for s in samples[-_TIMELINE_POINTS:]
        ]
        components.append({
            "component": comp,
            "uptime_pct": round(up / len(measured) * 100, 2) if measured else None,
            "current": latest.status if latest else "unknown",
            "detail": latest.detail if latest else None,
            "latency_ms": latest.latency_ms if latest else None,
            "last_checked": latest.ts.isoformat() if latest else None,
            "sample_count": len(samples),
            "timeline": timeline,
        })

    return {"window": win, "since": since.isoformat(), "components": components}
