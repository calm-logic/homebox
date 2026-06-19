from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import httpx

from ..auth import require_session_api
from ..db import get_session
from ..models import Organization, Repository
from ..crypto import encrypt
from ..github import get_org
from ..orgs import sync_org_repos
from ..webhooks_lib import sync_repo_webhook
from .. import deploy as engine

router = APIRouter(prefix="/api/organizations")


def _serialize(o: Organization) -> dict:
    return {
        "id": o.id,
        "login": o.login,
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "source": "oauth" if (o.pat_encrypted or "").startswith("oauth:") else "pat",
    }


class ConnectPatBody(BaseModel):
    login: str
    pat: str


@router.get("")
async def list_orgs(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(select(Organization).order_by(Organization.login))).scalars().all()
    return [_serialize(o) for o in rows]


@router.post("/connect-pat")
async def connect_pat(
    body: ConnectPatBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    login = body.login.strip().lstrip("@")
    pat = body.pat.strip()
    if not login or not pat:
        raise HTTPException(400, "Organization login and PAT are required")
    try:
        await get_org(pat, login)
    except httpx.HTTPStatusError as e:
        raise HTTPException(400, f"GitHub rejected the org/PAT: {e.response.status_code}")

    existing = (await session.execute(select(Organization).where(Organization.login == login))).scalar_one_or_none()
    if existing:
        existing.pat_encrypted = encrypt(pat)
    else:
        existing = Organization(login=login, pat_encrypted=encrypt(pat))
        session.add(existing)
    await session.commit()

    # Pull in repos immediately so the user doesn't have to click "Sync repos".
    # Best-effort — a sync failure shouldn't fail the connect.
    try:
        await sync_org_repos(session, existing)
        await session.commit()
    except httpx.HTTPStatusError:
        await session.rollback()

    return _serialize(existing)


@router.post("/{login}/sync")
async def sync_org(
    login: str,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    org = (await session.execute(select(Organization).where(Organization.login == login))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Org not found")

    try:
        count = await sync_org_repos(session, org)
    except httpx.HTTPStatusError as e:
        raise HTTPException(400, f"GitHub error: {e.response.status_code}")
    await session.commit()
    return {"ok": True, "synced": count}


@router.delete("/{login}")
async def disconnect_org(
    login: str,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    org = (await session.execute(select(Organization).where(Organization.login == login))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Org not found")

    # Tear down this org's managed project stacks (keep volumes — data survives a
    # reconnect) and remove their push webhooks while the token is still usable.
    managed = (await session.execute(
        select(Repository).where(Repository.organization_id == org.id, Repository.managed == True)  # noqa: E712
    )).scalars().all()
    for repo in managed:
        if repo.project_slug:
            await engine.teardown_stack(repo.project_slug)
        repo.managed = False
        await session.flush()
        await sync_repo_webhook(session, repo)  # best-effort hook removal

    await session.delete(org)  # cascades to repositories -> deployments
    await session.commit()
    return {"ok": True}
