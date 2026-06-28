from datetime import datetime
from fastapi import APIRouter, Depends
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
import httpx

from ..auth import require_session_api
from ..db import get_session
from ..models import Integration, Project, WorkflowRunCache
from ..integrations_lib import decrypted_token
from ..github import list_workflow_runs

router = APIRouter(prefix="/api/workflows")


def _serialize(r: WorkflowRunCache) -> dict:
    return {
        "id": r.id,
        "repository_full_name": r.repository_full_name,
        "name": r.name,
        "status": r.status,
        "conclusion": r.conclusion,
        "head_branch": r.head_branch,
        "html_url": r.html_url,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    }


@router.get("")
async def list_runs(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(
        select(WorkflowRunCache).order_by(WorkflowRunCache.created_at.desc()).limit(50)
    )).scalars().all()
    return [_serialize(r) for r in rows]


@router.post("/refresh")
async def refresh_runs(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    orgs = (await session.execute(
        select(Integration).where(Integration.provider == "github")
    )).scalars().all()
    token_by_org = {o.account_login: decrypted_token(o) for o in orgs if o.account_login}

    projects = (await session.execute(
        select(Project).where(Project.managed == True)  # noqa: E712
    )).scalars().all()
    fetched: list[tuple[int | None, str, dict]] = []
    for project in projects:
        owner = project.repo_full_name.split("/", 1)[0]
        token = token_by_org.get(owner)
        if not token:
            continue
        try:
            runs = await list_workflow_runs(token, project.repo_full_name, per_page=10)
        except httpx.HTTPStatusError:
            continue
        fetched.extend((project.id, project.repo_full_name, r) for r in runs)

    await session.execute(delete(WorkflowRunCache))
    for project_id, full_name, r in fetched:
        try:
            created = datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")).replace(tzinfo=None)
        except (KeyError, ValueError):
            continue
        session.add(WorkflowRunCache(
            project_id=project_id,
            repository_full_name=full_name,
            run_id=r["id"],
            name=r.get("name") or r.get("display_title") or "",
            status=r.get("status") or "",
            conclusion=r.get("conclusion"),
            head_branch=r.get("head_branch") or "",
            html_url=r.get("html_url") or "",
            created_at=created,
        ))
    await session.commit()
    return {"ok": True, "fetched": len(fetched)}
