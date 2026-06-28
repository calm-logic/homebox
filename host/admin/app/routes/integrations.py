"""Integrations API — connections to external systems (GitHub / GitLab /
Cloudflare). Lists every Integration row, and handles GitHub connect-via-PAT,
repo sync, and disconnect. GitHub OAuth connect lives in routes/oauth.py;
Cloudflare connect/disconnect lives in routes/tunnel.py — both write Integration
rows that show up here.
"""

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import deploy as engine
from ..auth import require_session_api
from ..crypto import encrypt
from ..db import get_session
from ..github import get_org
from ..integrations_lib import sync_github_projects
from ..models import Integration, Project
from ..webhooks_lib import sync_project_webhook

router = APIRouter(prefix="/api/integrations")


def _serialize(i: Integration, project_count: int = 0) -> dict:
    return {
        "id": i.id,
        "provider": i.provider,
        "account_login": i.account_login,
        "account_id": i.account_id,
        "name": i.name,
        "status": i.status,
        "source": "oauth" if (i.secret_encrypted or "").startswith("oauth:") else "pat"
        if i.provider != "cloudflare" else "token",
        "project_count": project_count,
        "created_at": i.created_at.isoformat() if i.created_at else None,
        "last_verified_at": i.last_verified_at.isoformat() if i.last_verified_at else None,
    }


@router.get("")
async def list_integrations(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(select(Integration).order_by(Integration.provider, Integration.account_login))).scalars().all()
    counts: dict[int, int] = {}
    for (iid,) in (await session.execute(select(Project.integration_id))).all():
        if iid is not None:
            counts[iid] = counts.get(iid, 0) + 1
    return [_serialize(i, counts.get(i.id, 0)) for i in rows]


class ConnectPatBody(BaseModel):
    login: str
    pat: str


@router.post("/github/connect-pat")
async def connect_github_pat(
    body: ConnectPatBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    login = body.login.strip().lstrip("@")
    pat = body.pat.strip()
    if not login or not pat:
        raise HTTPException(400, "Organization login and PAT are required")
    try:
        org = await get_org(pat, login)
    except httpx.HTTPStatusError as e:
        raise HTTPException(400, f"GitHub rejected the org/PAT: {e.response.status_code}")

    existing = (await session.execute(
        select(Integration).where(Integration.provider == "github", Integration.account_login == login)
    )).scalar_one_or_none()
    if existing:
        existing.secret_encrypted = encrypt(pat)
        existing.status = "connected"
        row = existing
    else:
        row = Integration(
            provider="github", account_login=login, account_id=str(org.get("id") or ""),
            name=login, secret_encrypted=encrypt(pat), status="connected",
        )
        session.add(row)
    await session.commit()

    try:
        await sync_github_projects(session, row)
        await session.commit()
    except httpx.HTTPStatusError:
        await session.rollback()

    return _serialize(row)


@router.post("/{integration_id}/sync")
async def sync_integration(
    integration_id: int,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    integ = await session.get(Integration, integration_id)
    if not integ:
        raise HTTPException(404, "Integration not found")
    if integ.provider != "github":
        raise HTTPException(400, "Only GitHub integrations sync repositories.")
    try:
        count = await sync_github_projects(session, integ)
    except httpx.HTTPStatusError as e:
        raise HTTPException(400, f"GitHub error: {e.response.status_code}")
    await session.commit()
    return {"ok": True, "synced": count}


@router.delete("/{integration_id}")
async def disconnect_integration(
    integration_id: int,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    integ = await session.get(Integration, integration_id)
    if not integ:
        raise HTTPException(404, "Integration not found")

    # Tear down this integration's managed project stacks (keep volumes) and
    # remove their push webhooks while the token is still usable.
    projects = (await session.execute(
        select(Project).where(Project.integration_id == integ.id, Project.managed == True)  # noqa: E712
    )).scalars().all()
    for project in projects:
        from .projects import _project_envs  # local import to avoid a cycle
        for env in await _project_envs(session, project.id):
            await engine.teardown_stack(project.name, env.name)
        project.managed = False
        await session.flush()
        await sync_project_webhook(session, project)

    await session.delete(integ)  # cascades to projects -> environments/services
    await session.commit()
    return {"ok": True}
