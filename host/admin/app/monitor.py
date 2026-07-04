"""Background infrastructure monitor + self-healer.

Every MONITOR_INTERVAL seconds it:
  1. Reconciles the local edge containers — if the cloudflared connector is down
     but we still hold its connector token, it is relaunched; if traefik or the
     docker-socket proxy are stopped, they're restarted.
  2. Probes the tunnel's connection state at Cloudflare's edge (via the API).
  3. Probes the public admin URL end-to-end (through the tunnel) if onboarding
     set one.
  4. Records one UptimeSample per component so the Tunnel page can show uptime %
     and a status timeline, and prunes samples past RETENTION.

The admin app runs *inside* Docker, so it cannot heal a dead daemon — that's the
job of the host boot unit (host-provisioner/homebox-boot.sh). This monitor heals
the case the daemon is up but a piece of the stack drifted, which is the common
post-reboot failure (cloudflared reaped, traefik racing the proxy, etc.).

Mirrors metrics.py's task lifecycle (start/stop, cancel-safe loop). Docker socket
calls and the cloudflared (re)launch are blocking, so they run in worker threads.
"""

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

import httpx
from sqlalchemy import delete, select

from . import cloudflare as cf
from .db import SessionLocal
from .deploy import verify_instances
from .host import container_status, restart_container, run_cloudflared_remote
from .models import Deployment, Domain, Setting, UptimeSample

log = logging.getLogger("homebox.monitor")

MONITOR_INTERVAL = 30          # seconds between health cycles
RETENTION = timedelta(days=14)  # how long to keep uptime samples
PRUNE_EVERY = 120              # prune once per this many cycles (~1h)

CLOUDFLARED = "homebox-cloudflared"
TRAEFIK = "homebox-traefik"
DOCKER_PROXY = "homebox-docker-proxy"
ADMIN_DOMAIN_KEY = "admin_domain"


async def _get_setting(session, key: str):
    row = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    return row.value if row else None


async def _set_setting(session, key: str, value) -> None:
    row = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    await session.commit()


async def _finish_pending_zones(session, state: dict) -> None:
    """Domains whose Cloudflare zone was created by Homebox sit in
    zone_status='pending' until the registrar NS change lands. Poll, and the
    moment Cloudflare reports the zone active, finish the wiring (DNS records +
    ingress) without the user coming back."""
    pending = (await session.execute(
        select(Domain).where(Domain.zone_status == "pending")
    )).scalars().all()
    if not pending:
        return
    token = cf.get_token(state)
    tunnel_id = state.get("tunnel_id")
    if not token:
        return
    for d in pending:
        try:
            zone = await cf.get_zone(token, d.zone_id) if d.zone_id else None
        except cf.CloudflareError:
            continue
        if not zone or (zone.get("status") or "").lower() != "active":
            continue
        log.info("zone %s is active — finishing DNS + ingress wiring", d.name)
        if tunnel_id:
            target = cf.tunnel_target(tunnel_id)
            try:
                await cf.upsert_cname(token, d.zone_id, d.name, target, proxied=True)
                await cf.upsert_cname(token, d.zone_id, f"*.{d.name}", target, proxied=True)
                d.cloudflare_routed = True
            except cf.CloudflareError as e:
                log.warning("zone %s active but DNS wiring failed: %s", d.name, e)
                continue
        d.zone_status = "active"
        await session.commit()
        try:
            from .routes.domains import _push_remote_ingress
            await _push_remote_ingress(session)
        except cf.CloudflareError as e:
            log.warning("ingress push after zone activation failed: %s", e)


async def _dns_drift_check(session, state: dict) -> tuple[str, str | None]:
    """Hourly: auto-repair stale tunnel CNAMEs (same logic as the manual Repair)
    and persist a summary the Domains page shows as a banner only when broken."""
    from .routes.tunnel import _resync_dns
    result = await _resync_dns(state, session)
    issues = [f"{e['hostname']}: {e['error']}" for e in result.get("errors", [])]
    repaired = result.get("updated", [])
    await _set_setting(session, "dns_status", {
        "checked_at": datetime.utcnow().isoformat(),
        "in_sync": not issues,
        "repaired": repaired,
        "issues": issues,
    })
    if issues:
        return "down", f"{len(issues)} record(s) cannot be repaired"
    if repaired:
        return "degraded", f"repointed {len(repaired)} stale record(s)"
    return "up", None


def _running(name: str) -> bool:
    return bool(container_status(name).get("running"))


async def _reconcile_cloudflared(state: dict) -> tuple[str, str | None]:
    """Ensure the cloudflared connector is running. Returns (status, detail).
    If it's down but we hold a connector token, relaunch it from the token."""
    if await asyncio.to_thread(_running, CLOUDFLARED):
        return "up", None
    token = cf.get_connector_token(state)
    if not token:
        # No tunnel configured yet — not an outage, just unconfigured.
        return "unknown", "no connector token (tunnel not configured)"
    log.warning("cloudflared is down — relaunching from stored connector token")
    ok, msg = await asyncio.to_thread(run_cloudflared_remote, token)
    if ok:
        return "degraded", "connector was down; relaunched"
    return "down", f"relaunch failed: {msg}"


async def _reconcile_container(name: str) -> tuple[str, str | None]:
    """Restart a stopped-but-existing edge container. Missing containers can't be
    recreated here (they're compose-owned) — the boot unit handles that."""
    st = await asyncio.to_thread(container_status, name)
    if st.get("running"):
        return "up", None
    if not st.get("exists"):
        return "down", "container missing (run the boot unit / compose up)"
    log.warning("%s is %s — restarting", name, st.get("state"))
    ok, msg = await asyncio.to_thread(restart_container, name)
    return ("degraded", "was stopped; restarted") if ok else ("down", f"restart failed: {msg}")


async def _probe_tunnel_edge(state: dict) -> tuple[str, str | None]:
    """Ask Cloudflare whether the tunnel has live connections at the edge."""
    token = cf.get_token(state)
    account_id = state.get("account_id")
    tunnel_id = state.get("tunnel_id")
    if not token or not account_id or not tunnel_id:
        return "unknown", "tunnel not configured"
    try:
        tunnel = await cf.get_tunnel(token, account_id, tunnel_id)
    except cf.CloudflareError as e:
        return "down", f"cloudflare api: {e}"
    conns = tunnel.get("connections") or []
    cf_status = (tunnel.get("status") or "").lower()
    n = len(conns) if isinstance(conns, list) else 0
    if cf_status == "healthy" or n > 0:
        return "up", f"{n} edge connection(s)"
    if cf_status in ("degraded", "inactive"):
        return "degraded", f"cloudflare reports '{cf_status}'"
    return "down", f"no edge connections (status '{cf_status or 'unknown'}')"


async def _probe_admin_url(admin_domain: str) -> tuple[str, str | None, int | None]:
    """End-to-end probe of the public admin URL through the tunnel."""
    url = f"https://{admin_domain.strip().strip('/')}/api/healthz"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get(url)
        latency = int(r.elapsed.total_seconds() * 1000)
        if r.status_code == 200:
            return "up", None, latency
        return "degraded", f"HTTP {r.status_code}", latency
    except httpx.HTTPError as e:
        return "down", f"{type(e).__name__}: {str(e)[:160]}", None


async def _cycle(cycle: int = 0) -> None:
    async with SessionLocal() as session:
        state = await cf.load_state(session)
        admin_domain = await _get_setting(session, ADMIN_DOMAIN_KEY)

        samples: list[UptimeSample] = []

        cf_status, cf_detail = await _reconcile_cloudflared(state)
        samples.append(UptimeSample(component="cloudflared", status=cf_status, detail=cf_detail))

        for comp, name in (("traefik", TRAEFIK), ("docker_proxy", DOCKER_PROXY)):
            st, detail = await _reconcile_container(name)
            samples.append(UptimeSample(component=comp, status=st, detail=detail))

        t_status, t_detail = await _probe_tunnel_edge(state)
        samples.append(UptimeSample(component="tunnel", status=t_status, detail=t_detail))

        if isinstance(admin_domain, str) and admin_domain.strip():
            a_status, a_detail, latency = await _probe_admin_url(admin_domain)
            samples.append(UptimeSample(
                component="admin_url", status=a_status, detail=a_detail, latency_ms=latency,
            ))

        # NS-pending zones: poll every ~5 min; drift check hourly (and on boot).
        if cycle % 10 == 0:
            try:
                await _finish_pending_zones(session, state)
            except Exception:  # noqa: BLE001
                log.exception("pending-zone check failed")
        if cycle % 120 == 0 and cf.get_token(state) and state.get("tunnel_id"):
            try:
                d_status, d_detail = await _dns_drift_check(session, state)
                samples.append(UptimeSample(component="dns", status=d_status, detail=d_detail))
            except Exception:  # noqa: BLE001
                log.exception("dns drift check failed")

        session.add_all(samples)
        await session.commit()

        # Re-verify the public URLs of live deployments each cycle so instance
        # status (and the UI's Down state) tracks reality, not just the last
        # deploy-time check.
        running = (await session.execute(
            select(Deployment.id).where(Deployment.status == "running")
        )).scalars().all()
        for dep_id in running:
            await verify_instances(session, dep_id, attempts=1)

        # Check-gated deploys that never resolved (hung workflow, lost webhook
        # delivery) shouldn't wait forever.
        stale_cutoff = datetime.utcnow() - timedelta(minutes=30)
        stuck = (await session.execute(
            select(Deployment).where(
                Deployment.status == "pending_checks",
                Deployment.created_at < stale_cutoff,
            )
        )).scalars().all()
        for d in stuck:
            d.status = "blocked"
            d.error = "Checks did not complete within 30 minutes."
        promo_cutoff = datetime.utcnow() - timedelta(minutes=60)
        stuck_promo = (await session.execute(
            select(Deployment).where(
                Deployment.status.in_(("pending_promotion", "pending_e2e")),
                Deployment.created_at < promo_cutoff,
            )
        )).scalars().all()
        for d in stuck_promo:
            d.status = "blocked"
            d.error = ("Promotion did not complete within 60 minutes "
                       "(source deploy or e2e workflow never finished).")
        if stuck or stuck_promo:
            await session.commit()


async def _prune() -> None:
    cutoff = datetime.utcnow() - RETENTION
    async with SessionLocal() as session:
        await session.execute(delete(UptimeSample).where(UptimeSample.ts < cutoff))
        await session.commit()


async def monitor_loop() -> None:
    cycle = 0
    while True:
        try:
            await _cycle(cycle)
            cycle += 1
            if cycle % PRUNE_EVERY == 0:
                await _prune()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — a bad cycle must not kill the monitor
            log.exception("monitor cycle failed")
        await asyncio.sleep(MONITOR_INTERVAL)


def start() -> asyncio.Task:
    return asyncio.create_task(monitor_loop(), name="homebox-monitor")


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
