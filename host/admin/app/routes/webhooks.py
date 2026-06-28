"""GitHub push webhook receiver. Not session-authenticated — the HMAC signature
is the auth. A verified push to a managed repo's default branch triggers a
background redeploy."""

import hashlib
import hmac
import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import get_session
from ..models import Environment, Project
from ..webhooks_lib import get_or_create_webhook_secret
from .projects import queue_deploy

router = APIRouter(prefix="/api/webhooks")


def _verify(secret: str, raw: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/github")
async def github_webhook(
    request: Request,
    background: BackgroundTasks,
    session: AsyncSession = Depends(get_session),
):
    raw = await request.body()  # raw bytes BEFORE parsing — re-serializing breaks the HMAC
    secret = await get_or_create_webhook_secret(session)
    if not _verify(secret, raw, request.headers.get("X-Hub-Signature-256")):
        raise HTTPException(401, "Invalid signature")

    event = request.headers.get("X-GitHub-Event", "")
    if event == "ping":
        return {"ok": True}
    if event != "push":
        return {"ok": True, "ignored": event}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    full_name = (payload.get("repository") or {}).get("full_name")
    ref = payload.get("ref") or ""
    if not full_name:
        return {"ok": True, "ignored": "no repository"}
    if not ref.startswith("refs/heads/"):
        return {"ok": True, "ignored": f"ref {ref}"}
    pushed_branch = ref[len("refs/heads/"):]

    project = (await session.execute(
        select(Project).where(Project.repo_full_name == full_name)
    )).scalar_one_or_none()
    if not project or not project.managed or not project.auto_deploy:
        return {"ok": True, "ignored": "project not managed / auto-deploy off"}

    # Deploy every environment whose branch matches the push. An env with no
    # branch set tracks the project's default branch.
    envs = (await session.execute(
        select(Environment).where(Environment.project_id == project.id)
    )).scalars().all()
    deployed = []
    for env in envs:
        target = env.branch or project.default_branch
        if target == pushed_branch:
            dep = await queue_deploy(session, background, env, trigger="webhook")
            deployed.append({"environment": env.name, "deployment_id": dep.id})
    if not deployed:
        return {"ok": True, "ignored": f"no environment tracks branch {pushed_branch}"}
    return {"ok": True, "deployed": deployed}
