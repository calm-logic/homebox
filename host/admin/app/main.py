import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from sqlalchemy import select, update

from . import clusterlib, metrics, migrate, monitor
from .auth import RequiresLogin
from .config import settings
from .db import SessionLocal
from .models import Domain, Deployment, Identity
from .routes import (
    auth as auth_routes,
    cluster as cluster_routes,
    dashboard,
    domains,
    identities as identities_routes,
    integrations,
    oauth as oauth_routes,
    onboarding,
    peer as peer_routes,
    projects,
    provision as provision_routes,
    runner,
    services,
    theme,
    webhooks,
    workflows,
    tunnel,
)

# Deployment statuses that are not terminal — a process must be actively driving
# them. Any left behind after a restart was interrupted and can never resume.
_ACTIVE_DEPLOY_STATUSES = ("queued", "cloning", "dissecting", "building", "starting")


async def _seed_primary_domain() -> None:
    """If the host was provisioned with a HOMEBOX_DOMAIN, ensure a primary
    wildcard Domain row exists for it. Idempotent — called every startup."""
    root = (settings.homebox_domain or "").strip().lower().strip(".")
    if not root:
        return
    async with SessionLocal() as session:
        existing = (await session.execute(select(Domain).where(Domain.name == root))).scalar_one_or_none()
        if existing:
            if not existing.is_primary:
                # Promote it (and demote any other primaries).
                for d in (await session.execute(select(Domain).where(Domain.is_primary == True))).scalars():
                    d.is_primary = False
                existing.is_primary = True
                await session.commit()
            return
        # No row at all yet — create it as the primary wildcard.
        for d in (await session.execute(select(Domain).where(Domain.is_primary == True))).scalars():
            d.is_primary = False
        session.add(Domain(name=root, is_primary=True, cloudflare_routed=True))
        await session.commit()


async def _seed_identities() -> None:
    """Seed whitelisted login emails from the host-mounted secrets.json
    (`identities` array, set at install time). Idempotent — adds any missing
    rows on every startup, never touches existing ones (so a disabled identity
    stays disabled)."""
    try:
        data = json.loads(settings.homebox_secrets_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError, PermissionError):
        return
    emails = (data or {}).get("identities") or []
    seed = {e.strip().lower() for e in emails if isinstance(e, str) and e.strip()}
    if not seed:
        return
    async with SessionLocal() as session:
        existing = set((await session.execute(select(Identity.email))).scalars().all())
        added = False
        for email in seed - existing:
            session.add(Identity(email=email, enabled=True))
            added = True
        if added:
            await session.commit()


async def _fail_interrupted_deployments() -> None:
    """A deploy runs in a background task; if the worker restarted mid-deploy the
    row is stuck in a non-terminal state with nothing driving it. Mark such rows
    failed so the UI doesn't show a perpetual 'building'."""
    async with SessionLocal() as session:
        await session.execute(
            update(Deployment)
            .where(Deployment.status.in_(_ACTIVE_DEPLOY_STATUSES))
            .values(status="failed", error="Interrupted by an admin restart.")
        )
        await session.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bring the admin DB to head. Alembic is synchronous, so run it off the event
    # loop. On pre-Alembic databases this auto-adopts (reconcile → stamp 0001 →
    # upgrade); see app/migrate.py. Replaces the old create_all + ADD COLUMN block.
    await asyncio.to_thread(migrate.run_migrations)
    settings.projects_host_dir.mkdir(parents=True, exist_ok=True)
    await _fail_interrupted_deployments()
    await _seed_primary_domain()
    await _seed_identities()
    sampler = metrics.start()
    # The monitor's first cycle runs immediately (no initial sleep), so starting
    # it here also does the post-restart reconcile (relaunch cloudflared, etc.).
    health = monitor.start()
    # Cluster heartbeat/sync loop (no-op while the node isn't in a cluster).
    cluster_task = clusterlib.start()
    # Mirror nodes also run the fast failover prober (seconds-scale promote;
    # the slow loop above keeps demote + acts as backstop).
    probe_task = clusterlib.start_mirror_probe() if settings.node_role == "mirror" else None
    try:
        yield
    finally:
        await metrics.stop(sampler)
        await monitor.stop(health)
        await clusterlib.stop(cluster_task)
        if probe_task:
            await clusterlib.stop(probe_task)


app = FastAPI(title="Homebox Admin", lifespan=lifespan, docs_url="/api/docs", openapi_url="/api/openapi.json")

# JSON API routers
app.include_router(auth_routes.router)
app.include_router(dashboard.router)
app.include_router(domains.router)
app.include_router(integrations.router)
app.include_router(projects.router)
app.include_router(services.router)
app.include_router(webhooks.router)
app.include_router(runner.router)
app.include_router(workflows.router)
app.include_router(tunnel.router)
app.include_router(oauth_routes.router)
app.include_router(onboarding.router)
app.include_router(identities_routes.router)
app.include_router(theme.router)
app.include_router(cluster_routes.router)
app.include_router(provision_routes.router)
app.include_router(peer_routes.router)


@app.get("/api/healthz")
async def healthz():
    return {"ok": True}


# Backwards-compat: convert RequiresLogin to JSON 401 (used to render to /login
# but the SPA handles routing now).
@app.exception_handler(RequiresLogin)
async def requires_login_handler(_request: Request, _exc: RequiresLogin):
    return JSONResponse({"detail": "Authentication required"}, status_code=401)


# ── SPA static asset mount + index fallback ──────────────────────────────────
SPA_DIR = Path(__file__).resolve().parent.parent / "static_spa"

# Serve hashed bundle assets
if (SPA_DIR / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(SPA_DIR / "assets")), name="spa-assets")


@app.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str):
    # API/openapi/healthz are handled above; everything else is the SPA.
    if full_path.startswith("api/") or full_path == "api":
        raise HTTPException(404, "Not Found")
    candidate = SPA_DIR / full_path
    if full_path and candidate.is_file():
        return FileResponse(candidate)
    index = SPA_DIR / "index.html"
    if index.is_file():
        return FileResponse(index)
    return JSONResponse(
        {"detail": "SPA bundle not built. Run `make admin` to rebuild the admin image."},
        status_code=503,
    )
