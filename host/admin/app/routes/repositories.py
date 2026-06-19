import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_session_api
from ..db import get_session
from ..models import Deployment, Repository
from ..webhooks_lib import sync_repo_webhook

router = APIRouter(prefix="/api/repositories")

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _deployment_summary(d: Deployment | None) -> dict | None:
    if not d:
        return None
    return {
        "status": d.status,
        "url": d.url,
        "commit_sha": d.commit_sha,
        "error": d.error,
        "trigger": d.trigger,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


def _serialize(r: Repository, deployment: Deployment | None = None) -> dict:
    return {
        "id": r.id,
        "full_name": r.full_name,
        "default_branch": r.default_branch,
        "project_slug": r.project_slug,
        "managed": r.managed,
        "deployment": _deployment_summary(deployment),
    }


class BindBody(BaseModel):
    project_slug: str | None = None
    managed: bool | None = None


async def _latest_deployments(session: AsyncSession, repo_ids: list[int]) -> dict[int, Deployment]:
    """Map repository_id -> its most recent Deployment row."""
    if not repo_ids:
        return {}
    rows = (await session.execute(
        select(Deployment)
        .where(Deployment.repository_id.in_(repo_ids))
        .order_by(Deployment.created_at.desc())
    )).scalars().all()
    latest: dict[int, Deployment] = {}
    for d in rows:
        latest.setdefault(d.repository_id, d)  # first seen = newest (desc order)
    return latest


@router.get("")
async def list_repos(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    rows = (await session.execute(select(Repository).order_by(Repository.full_name))).scalars().all()
    latest = await _latest_deployments(session, [r.id for r in rows])
    return [_serialize(r, latest.get(r.id)) for r in rows]


@router.post("/{repo_id}/bind")
async def bind_repo(
    repo_id: int,
    body: BindBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    repo = await session.get(Repository, repo_id)
    if not repo:
        raise HTTPException(404, "Repository not found")

    slug = (body.project_slug or "").strip().lower() or None
    managed = repo.managed if body.managed is None else body.managed

    if managed:
        if not slug:
            slug = repo.full_name.split("/")[-1].lower()
        if not SLUG_RE.match(slug):
            raise HTTPException(400, "Slug must be lowercase letters, numbers, and hyphens (1–63 chars).")
        clash = (await session.execute(
            select(Repository).where(Repository.project_slug == slug, Repository.id != repo.id)
        )).scalar_one_or_none()
        if clash:
            raise HTTPException(409, f"Slug '{slug}' is already used by {clash.full_name}.")

    managed_changed = repo.managed != managed
    repo.project_slug = slug
    repo.managed = managed
    await session.commit()
    await session.refresh(repo)

    # Register (or remove) the push webhook when the managed flag flips.
    webhook_note: str | None = None
    if managed_changed:
        _ok, webhook_note = await sync_repo_webhook(session, repo)

    latest = await _latest_deployments(session, [repo.id])
    result = _serialize(repo, latest.get(repo.id))
    if webhook_note:
        result["webhook_note"] = webhook_note
    return result
