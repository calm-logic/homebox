"""Services API — per-service overrides, env vars, and resource metrics.

Services themselves are created by dissection (routes/projects.py). Here the user
can flip a service public/private, change its subdomain label, and set/override
env vars (stored as ServiceEnvVar source='user', layered over the auto-wired
connection vars at deploy time).
"""

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_session_api
from ..db import get_session
from ..models import MetricSample, Service, ServiceEnvVar

router = APIRouter(prefix="/api/services")

_WINDOWS = {
    "1h": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
}
_VALID_KINDS = ("web", "api", "database", "cache", "worker", "static", "other")


async def _get_service(session: AsyncSession, service_id: int) -> Service:
    svc = await session.get(Service, service_id)
    if not svc:
        raise HTTPException(404, "Service not found")
    return svc


class PatchServiceBody(BaseModel):
    is_public: bool | None = None
    subdomain_label: str | None = None
    kind: str | None = None
    internal_port: int | None = None


@router.patch("/{service_id}")
async def patch_service(
    service_id: int,
    body: PatchServiceBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    svc = await _get_service(session, service_id)
    if body.is_public is not None:
        svc.is_public = body.is_public
    if body.subdomain_label is not None:
        svc.subdomain_label = body.subdomain_label.strip().lower()
    if body.kind is not None:
        if body.kind not in _VALID_KINDS:
            raise HTTPException(400, f"kind must be one of {_VALID_KINDS}")
        svc.kind = body.kind
    if body.internal_port is not None:
        svc.internal_port = body.internal_port
    await session.commit()
    return {"ok": True, "id": svc.id}


class EnvVar(BaseModel):
    key: str
    value: str = ""
    is_secret: bool = False
    environment_id: int | None = None


class SetEnvBody(BaseModel):
    vars: list[EnvVar]


@router.put("/{service_id}/env-vars")
async def set_env_vars(
    service_id: int,
    body: SetEnvBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Replace all user-set env vars for a service. Auto-wired (source='auto')
    vars are left untouched."""
    svc = await _get_service(session, service_id)
    await session.execute(
        delete(ServiceEnvVar).where(
            ServiceEnvVar.service_id == svc.id, ServiceEnvVar.source == "user"
        )
    )
    for v in body.vars:
        key = v.key.strip()
        if not key:
            continue
        session.add(ServiceEnvVar(
            service_id=svc.id, environment_id=v.environment_id,
            key=key, value=v.value, source="user", is_secret=v.is_secret,
        ))
    await session.commit()
    return {"ok": True, "count": len(body.vars)}


@router.get("/{service_id}/metrics")
async def service_metrics(
    service_id: int,
    window: str = "1h",
    environment_id: int | None = None,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    await _get_service(session, service_id)
    lookback = _WINDOWS.get(window, _WINDOWS["1h"])
    since = datetime.utcnow() - lookback
    q = select(MetricSample).where(
        MetricSample.service_id == service_id, MetricSample.ts >= since
    )
    if environment_id is not None:
        q = q.where(MetricSample.environment_id == environment_id)
    rows = (await session.execute(q.order_by(MetricSample.ts.asc()))).scalars().all()

    points = []
    prev = None
    for s in rows:
        rx_bps = tx_bps = 0.0
        if prev is not None:
            dt = (s.ts - prev.ts).total_seconds()
            if dt > 0:
                rx_bps = max(s.net_rx - prev.net_rx, 0) / dt
                tx_bps = max(s.net_tx - prev.net_tx, 0) / dt
        points.append({
            "ts": s.ts.isoformat(), "cpu_pct": s.cpu_pct,
            "mem_used": s.mem_used, "mem_limit": s.mem_limit,
            "net_rx_bps": round(rx_bps, 1), "net_tx_bps": round(tx_bps, 1),
        })
        prev = s
    return {"window": window if window in _WINDOWS else "1h", "points": points}
