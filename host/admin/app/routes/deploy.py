from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_session_api
from ..db import get_session
from .. import deploy as engine
from ..host import container_status
from ..models import Deployment, Domain, MetricSample, Repository, WorkflowRunCache

router = APIRouter(prefix="/api/repositories")

# Supported chart windows → lookback. Default 1h.
_WINDOWS = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}


async def _latest_deployment(session: AsyncSession, repo_id: int) -> Deployment | None:
    return (await session.execute(
        select(Deployment)
        .where(Deployment.repository_id == repo_id)
        .order_by(Deployment.created_at.desc())
        .limit(1)
    )).scalar_one_or_none()


async def queue_deploy(session: AsyncSession, background: BackgroundTasks,
                       repo: Repository, *, trigger: str) -> Deployment:
    """Create a queued Deployment and schedule run_deploy in the background.
    Shared by the manual endpoint and the push webhook."""
    dep = Deployment(
        repository_id=repo.id,
        slug=repo.project_slug,
        status="queued",
        stack_name=engine.stack_name(repo.project_slug),
        trigger=trigger,
    )
    session.add(dep)
    await session.commit()
    await session.refresh(dep)
    background.add_task(engine.run_deploy, dep.id, trigger=trigger)
    return dep


@router.post("/{repo_id}/deploy")
async def trigger_deploy(
    repo_id: int,
    background: BackgroundTasks,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    repo = await session.get(Repository, repo_id)
    if not repo:
        raise HTTPException(404, "Repository not found")
    if not repo.managed or not repo.project_slug:
        raise HTTPException(400, "Enable 'Managed by Homebox' and set a slug before deploying.")

    primary = (await session.execute(
        select(Domain).where(Domain.is_primary == True)  # noqa: E712
    )).scalar_one_or_none()
    if not primary:
        raise HTTPException(400, "No primary domain configured. Add one under Domains first.")

    dep = await queue_deploy(session, background, repo, trigger="manual")
    return {
        "id": dep.id,
        "status": dep.status,
        "url": f"https://{repo.project_slug}.{primary.name}",
    }


@router.get("/{repo_id}/deployment")
async def get_deployment(
    repo_id: int,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    repo = await session.get(Repository, repo_id)
    if not repo:
        raise HTTPException(404, "Repository not found")
    dep = await _latest_deployment(session, repo_id)
    if not dep:
        return {"deployment": None, "container": None}
    container = container_status(dep.web_container) if dep.web_container else None
    return {
        "deployment": {
            "id": dep.id,
            "status": dep.status,
            "url": dep.url,
            "commit_sha": dep.commit_sha,
            "error": dep.error,
            "log_tail": dep.log_tail,
            "trigger": dep.trigger,
            "updated_at": dep.updated_at.isoformat() if dep.updated_at else None,
        },
        "container": container,
    }


@router.get("/{repo_id}/metrics")
async def get_metrics(
    repo_id: int,
    window: str = "1h",
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    repo = await session.get(Repository, repo_id)
    if not repo:
        raise HTTPException(404, "Repository not found")
    lookback = _WINDOWS.get(window, _WINDOWS["1h"])
    since = datetime.utcnow() - lookback

    rows = (await session.execute(
        select(MetricSample)
        .where(MetricSample.repository_id == repo_id, MetricSample.ts >= since)
        .order_by(MetricSample.ts.asc())
    )).scalars().all()

    points = []
    prev = None
    for s in rows:
        rx_bps = tx_bps = 0.0
        if prev is not None:
            dt = (s.ts - prev.ts).total_seconds()
            if dt > 0:
                # Cumulative counters reset to 0 on container restart → clamp negatives.
                rx_bps = max(s.net_rx - prev.net_rx, 0) / dt
                tx_bps = max(s.net_tx - prev.net_tx, 0) / dt
        points.append({
            "ts": s.ts.isoformat(),
            "cpu_pct": s.cpu_pct,
            "mem_used": s.mem_used,
            "mem_limit": s.mem_limit,
            "net_rx_bps": round(rx_bps, 1),
            "net_tx_bps": round(tx_bps, 1),
        })
        prev = s
    return {"window": window if window in _WINDOWS else "1h", "points": points}


@router.get("/{repo_id}/workflows")
async def get_repo_workflows(
    repo_id: int,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    repo = await session.get(Repository, repo_id)
    if not repo:
        raise HTTPException(404, "Repository not found")
    rows = (await session.execute(
        select(WorkflowRunCache)
        .where(WorkflowRunCache.repository_full_name == repo.full_name)
        .order_by(WorkflowRunCache.created_at.desc())
        .limit(30)
    )).scalars().all()
    return [{
        "id": r.id,
        "run_id": r.run_id,
        "name": r.name,
        "status": r.status,
        "conclusion": r.conclusion,
        "head_branch": r.head_branch,
        "html_url": r.html_url,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


@router.post("/{repo_id}/stop")
async def stop_deploy(
    repo_id: int,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    repo = await session.get(Repository, repo_id)
    if not repo:
        raise HTTPException(404, "Repository not found")
    if not repo.project_slug:
        raise HTTPException(400, "Repository has no project slug.")
    ok, msg = await engine.teardown_stack(repo.project_slug)
    dep = await _latest_deployment(session, repo_id)
    if dep:
        dep.status = "stopped"
        await session.commit()
    return {"ok": ok, "message": msg}
