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
from ..models import MetricSample, Service, ServiceEnvVar, SECRET_MASK

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
    vars are left untouched. A secret submitted with the masked value
    (SECRET_MASK — what the GET returns instead of the real value) keeps its
    stored value: the UI shows the mask, and saving other fields must not
    overwrite the real secret with the placeholder."""
    svc = await _get_service(session, service_id)
    existing = (await session.execute(
        select(ServiceEnvVar).where(
            ServiceEnvVar.service_id == svc.id, ServiceEnvVar.source == "user"
        )
    )).scalars().all()
    prior = {(v.key, v.environment_id): v.value for v in existing}

    await session.execute(
        delete(ServiceEnvVar).where(
            ServiceEnvVar.service_id == svc.id, ServiceEnvVar.source == "user"
        )
    )
    for v in body.vars:
        key = v.key.strip()
        if not key:
            continue
        value = v.value
        if v.is_secret and value == SECRET_MASK:
            # Unchanged secret — restore the stored value; never persist the mask.
            value = prior.get((key, v.environment_id), "")
        session.add(ServiceEnvVar(
            service_id=svc.id, environment_id=v.environment_id,
            key=key, value=value, source="user", is_secret=v.is_secret,
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


# ───── Data browsing (database / cache services) ─────────────────────────────
# All access goes through `docker exec` into the service's own container using
# its own credentials (env vars) — the admin never needs drivers or network
# reachability into project stacks.

import json as _json
import re as _re

from sqlalchemy import desc as _desc

from ..deploy import _run as _docker_run, _router_name
from ..models import Deployment, Environment, Project, ServiceInstance

_IDENT_RE = _re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

_PSQL = (
    'exec psql -U "${POSTGRES_USER:-postgres}" '
    '-d "${POSTGRES_DB:-${POSTGRES_USER:-postgres}}" -At -c "$HB_SQL"'
)


async def _service_container(
    session: AsyncSession, svc: Service, environment_id: int,
) -> tuple[Environment, str]:
    env = await session.get(Environment, environment_id)
    if not env or env.project_id != svc.project_id:
        raise HTTPException(404, "Environment not found")
    dep = (await session.execute(
        select(Deployment).where(Deployment.environment_id == env.id)
        .order_by(_desc(Deployment.created_at)).limit(1)
    )).scalar_one_or_none()
    inst = None
    if dep:
        inst = (await session.execute(
            select(ServiceInstance).where(
                ServiceInstance.deployment_id == dep.id,
                ServiceInstance.service_name == svc.name,
            )
        )).scalar_one_or_none()
    if not inst or not inst.container_name:
        raise HTTPException(409, "No deployed container for this service in that environment.")
    return env, inst.container_name


def _data_flavor(svc: Service) -> str | None:
    blob = (svc.name + " " + " ".join((svc.env_template or {}).keys())).lower()
    if svc.kind == "database" and ("postgres" in blob or "pg" in blob):
        return "postgres"
    if svc.kind == "cache" and "redis" in blob or svc.name.lower() == "redis":
        return "redis"
    return None


async def _pg(container: str, sql: str) -> str:
    code, out = await _docker_run(
        ["docker", "exec", "-e", f"HB_SQL={sql}", container, "sh", "-c", _PSQL],
        timeout=20,
    )
    if code:
        raise HTTPException(502, f"query failed: {out[-500:]}")
    return out


@router.get("/{service_id}/data")
async def data_overview(
    service_id: int,
    environment_id: int,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """What's browsable: postgres → tables, redis → keyspaces."""
    svc = await _get_service(session, service_id)
    flavor = _data_flavor(svc)
    if not flavor:
        raise HTTPException(400, "Data browsing is only available for postgres and redis services.")
    _env, container = await _service_container(session, svc, environment_id)

    if flavor == "postgres":
        out = await _pg(
            container,
            "select tablename from pg_tables where schemaname='public' order by 1",
        )
        return {"flavor": "postgres", "tables": [t for t in out.splitlines() if t]}

    code, out = await _docker_run(
        ["docker", "exec", container, "sh", "-c",
         'exec redis-cli ${REDIS_PASSWORD:+-a "$REDIS_PASSWORD"} --no-auth-warning info keyspace'],
        timeout=15,
    )
    if code:
        raise HTTPException(502, f"redis info failed: {out[-300:]}")
    dbs = []
    for line in out.splitlines():
        m = _re.match(r"^db(\d+):keys=(\d+)", line.strip())
        if m:
            dbs.append({"index": int(m.group(1)), "keys": int(m.group(2))})
    if not dbs:
        dbs = [{"index": 0, "keys": 0}]
    return {"flavor": "redis", "dbs": dbs}


@router.get("/{service_id}/data/rows")
async def data_rows(
    service_id: int,
    environment_id: int,
    table: str,
    limit: int = 50,
    offset: int = 0,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    svc = await _get_service(session, service_id)
    if _data_flavor(svc) != "postgres":
        raise HTTPException(400, "Row browsing is only available for postgres services.")
    if not _IDENT_RE.match(table):
        raise HTTPException(400, "Invalid table name")
    _env, container = await _service_container(session, svc, environment_id)

    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    cols_raw = await _pg(
        container,
        "select coalesce(json_agg(column_name order by ordinal_position), '[]'::json) "
        f"from information_schema.columns where table_schema='public' and table_name='{table}'",
    )
    columns = _json.loads(cols_raw or "[]")
    if not columns:
        raise HTTPException(404, "Table not found")
    count_raw = await _pg(container, f'select count(*) from "{table}"')
    rows_raw = await _pg(
        container,
        "select coalesce(json_agg(t), '[]'::json) from "
        f'(select * from "{table}" limit {limit} offset {offset}) t',
    )
    return {
        "table": table, "columns": columns,
        "rows": _json.loads(rows_raw or "[]"),
        "total": int(count_raw.strip() or 0),
        "limit": limit, "offset": offset,
    }


@router.get("/{service_id}/data/keys")
async def data_keys(
    service_id: int,
    environment_id: int,
    db: int = 0,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    svc = await _get_service(session, service_id)
    if _data_flavor(svc) != "redis":
        raise HTTPException(400, "Key browsing is only available for redis services.")
    _env, container = await _service_container(session, svc, environment_id)
    db = max(0, min(db, 15))

    script = (
        'auth=${REDIS_PASSWORD:+-a "$REDIS_PASSWORD"}; '
        f'redis-cli $auth --no-auth-warning -n {db} --scan | head -200 | while IFS= read -r k; do '
        f'printf "%s\\t%s\\t%s\\n" "$k" '
        f'"$(redis-cli $auth --no-auth-warning -n {db} type "$k")" '
        f'"$(redis-cli $auth --no-auth-warning -n {db} ttl "$k")"; done'
    )
    code, out = await _docker_run(["docker", "exec", container, "sh", "-c", script], timeout=30)
    if code:
        raise HTTPException(502, f"redis scan failed: {out[-300:]}")
    keys = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            keys.append({"key": parts[0], "type": parts[1], "ttl": int(parts[2]) if parts[2].lstrip("-").isdigit() else None})
    return {"db": db, "keys": keys}


@router.get("/{service_id}/requests")
async def request_log(
    service_id: int,
    environment_id: int,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Recent requests for a public service, from Traefik's JSON access log."""
    svc = await _get_service(session, service_id)
    if not svc.is_public:
        raise HTTPException(400, "Request monitoring is only available for public services.")
    env = await session.get(Environment, environment_id)
    if not env or env.project_id != svc.project_id:
        raise HTTPException(404, "Environment not found")
    project = await session.get(Project, svc.project_id)

    base = _router_name(project.name, svc.subdomain_label, env.name)
    legacy = f"{project.name}-{svc.subdomain_label}" if svc.subdomain_label else project.name
    routers = {f"{n}@docker" for n in (base, f"{base}-path", legacy, f"{legacy}-path")}

    code, out = await _docker_run(["docker", "logs", "--tail", "3000", "homebox-traefik"], timeout=20)
    if code:
        raise HTTPException(502, f"could not read traefik logs: {out[-300:]}")
    requests = []
    for line in out.splitlines():
        if not line.startswith("{"):
            continue
        try:
            e = _json.loads(line)
        except ValueError:
            continue
        if e.get("RouterName") not in routers:
            continue
        requests.append({
            "time": e.get("StartUTC"),
            "method": e.get("RequestMethod"),
            "path": e.get("RequestPath"),
            "status": e.get("DownstreamStatus"),
            "duration_ms": round((e.get("Duration") or 0) / 1e6, 1),
            "client": (e.get("ClientHost") or ""),
        })
    requests.reverse()  # newest first
    return {"requests": requests[:200], "access_log_enabled": any(l.startswith("{") for l in out.splitlines())}
