from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_session_api
from ..db import get_session
from ..models import Deployment, Domain, Environment, Integration, MetricSample, Project
from ..host import list_runner_containers, runner_status

router = APIRouter(prefix="/api")

# Deployment.status → coarse bucket for the activity charts.
_SUCCESS_STATUSES = {"running", "stopped", "superseded"}
_FAILED_STATUSES = {"failed", "blocked"}
_BUILDING_STATUSES = {
    "queued", "cloning", "dissecting", "building", "starting",
    "pending_checks", "pending_promotion", "pending_e2e",
}


@router.get("/summary")
async def summary(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    integration_count = (await session.execute(select(func.count()).select_from(Integration))).scalar_one()
    project_count = (await session.execute(select(func.count()).select_from(Project))).scalar_one()
    managed_count = (await session.execute(
        select(func.count()).select_from(Project).where(Project.managed == True)  # noqa: E712
    )).scalar_one()
    domain_count = (await session.execute(select(func.count()).select_from(Domain))).scalar_one()
    runners = list_runner_containers()
    host = runner_status()
    return {
        "integration_count": integration_count,
        "project_count": project_count,
        "managed_count": managed_count,
        "domain_count": domain_count,
        "runner": {
            "installed": host.get("installed", False) or len(runners) > 0,
            "container_count": len(runners),
        },
    }


# Window → (span, bucket size). Bucket sizes are chosen to keep each series at
# ~30–60 points so the charts stay legible from a 1-hour zoom out to 30 days.
_EPOCH = datetime(1970, 1, 1)
_ACTIVITY_WINDOWS: dict[str, tuple[timedelta, int]] = {
    "1h": (timedelta(hours=1), 120),        # 2 min
    "6h": (timedelta(hours=6), 720),        # 12 min
    "24h": (timedelta(hours=24), 1800),     # 30 min
    "7d": (timedelta(days=7), 10800),       # 3 h
    "30d": (timedelta(days=30), 43200),     # 12 h
}


@router.get("/activity")
async def activity(
    window: str = "6h",
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Project-activity metrics for the Overview dashboard charts. `window`
    (1h|6h|24h|7d|30d) zooms every time-series: deploys and fleet CPU/memory
    share one bucketed time axis. The status mix is always "now"; headline
    totals (running envs, deploys in 7d) are window-independent. Bucketing is
    done in Python so it stays DB-agnostic."""
    span, bucket_secs = _ACTIVITY_WINDOWS.get(window, _ACTIVITY_WINDOWS["6h"])
    if window not in _ACTIVITY_WINDOWS:
        window = "6h"
    now = datetime.utcnow()
    start = now - span

    def bucket_start(ts: datetime) -> datetime:
        e = (ts - _EPOCH).total_seconds()
        return _EPOCH + timedelta(seconds=e - (e % bucket_secs))

    # Pre-build every bucket slot across the window so the axis is continuous
    # (empty slots render as gaps in the resource lines / zero-height bars).
    slots: list[datetime] = []
    t = bucket_start(start)
    end = bucket_start(now)
    while t <= end:
        slots.append(t)
        t += timedelta(seconds=bucket_secs)
    buckets = {
        s: {"ts": s.isoformat(), "cpu_pct": None, "mem_used": None, "succeeded": 0, "failed": 0}
        for s in slots
    }

    # ── Deployments (all-time fetch; homelab volumes are modest). Newest first
    # so the first row seen per environment is its current status. ──────────
    deploys = (await session.execute(
        select(
            Deployment.environment_id, Deployment.status, Deployment.created_at,
        ).order_by(Deployment.created_at.desc())
    )).all()
    env_project = dict((await session.execute(
        select(Environment.id, Environment.project_id)
    )).all())
    project_name = dict((await session.execute(
        select(Project.id, Project.name)
    )).all())

    since_7d = now - timedelta(days=7)
    deploys_7d = 0
    top_counts: dict[int, int] = defaultdict(int)
    breakdown = {"running": 0, "failed": 0, "building": 0, "idle": 0}
    seen_envs: set[int] = set()

    for env_id, status, created_at in deploys:
        if created_at >= since_7d:
            deploys_7d += 1
        # Current status mix: the latest deployment per environment (all-time).
        if env_id not in seen_envs:
            seen_envs.add(env_id)
            if status in _FAILED_STATUSES:
                breakdown["failed"] += 1
            elif status in _BUILDING_STATUSES:
                breakdown["building"] += 1
            elif status == "running":
                breakdown["running"] += 1
            else:
                breakdown["idle"] += 1
        # Windowed: deploy buckets + most-active projects.
        if created_at >= start:
            pid = env_project.get(env_id)
            if pid is not None:
                top_counts[pid] += 1
            b = buckets.get(bucket_start(created_at))
            if b is not None:
                if status in _SUCCESS_STATUSES:
                    b["succeeded"] += 1
                elif status in _FAILED_STATUSES:
                    b["failed"] += 1

    top_projects = sorted(
        ({"name": project_name.get(pid, f"#{pid}"), "deploys": n}
         for pid, n in top_counts.items()),
        key=lambda r: r["deploys"], reverse=True,
    )[:5]

    # ── Fleet resource usage over the window, summed across services (latest
    # sample per service per bucket). ──────────────────────────────────────
    samples = (await session.execute(
        select(
            MetricSample.service_id, MetricSample.ts,
            MetricSample.cpu_pct, MetricSample.mem_used,
        ).where(MetricSample.ts >= start).order_by(MetricSample.ts.asc())
    )).all()
    res_last: dict[datetime, dict[int, tuple[datetime, float, int]]] = defaultdict(dict)
    for service_id, ts, cpu_pct, mem_used in samples:
        svc = res_last[bucket_start(ts)]
        prev = svc.get(service_id)
        if prev is None or ts >= prev[0]:
            svc[service_id] = (ts, cpu_pct, mem_used)
    for slot, svc in res_last.items():
        b = buckets.get(slot)
        if b is not None:
            b["cpu_pct"] = round(sum(v[1] for v in svc.values()), 1)
            b["mem_used"] = sum(v[2] for v in svc.values())

    return {
        "window": window,
        "buckets": [buckets[s] for s in slots],
        "status_breakdown": breakdown,
        "top_projects": top_projects,
        "totals": {"deploys_7d": deploys_7d, "running_envs": breakdown["running"]},
    }
