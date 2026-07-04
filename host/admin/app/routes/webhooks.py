"""GitHub webhook receiver. Not session-authenticated — the HMAC signature
is the auth.

Two events drive check-gated auto-deploys:

    push          → if the project requires checks AND the repo has workflows,
                    record a `pending_checks` deployment (one per env tracking
                    the branch). Otherwise deploy immediately (old behavior).
    workflow_run  → on completion, promote pending deployments for that commit
                    when ALL check runs succeeded, or mark them `blocked` when
                    any failed. Repos with several workflows promote only once
                    the last one finishes.
"""

import hashlib
import hmac
import json

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import deploy as engine
from .. import github, urls
from ..db import get_session
from ..integrations_lib import decrypted_token
from ..models import Deployment, Environment, Integration, Project
from ..webhooks_lib import get_or_create_webhook_secret
from .projects import queue_deploy

router = APIRouter(prefix="/api/webhooks")

CHECKS_OK = ("success", "neutral", "skipped")


def _verify(secret: str, raw: bytes, signature: str | None) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _project_token(session: AsyncSession, project: Project) -> str | None:
    if not project.integration_id:
        return None
    integration = await session.get(Integration, project.integration_id)
    return decrypted_token(integration) if integration else None


async def _matching_envs(session: AsyncSession, project: Project, branch: str) -> list[Environment]:
    envs = (await session.execute(
        select(Environment).where(Environment.project_id == project.id)
    )).scalars().all()
    return [e for e in envs if (e.branch or project.default_branch) == branch]


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
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    if event == "push":
        return await _on_push(session, background, payload)
    if event == "workflow_run":
        return await _on_workflow_run(session, background, payload)
    return {"ok": True, "ignored": event}


async def _on_push(session: AsyncSession, background: BackgroundTasks, payload: dict):
    full_name = (payload.get("repository") or {}).get("full_name")
    ref = payload.get("ref") or ""
    sha = payload.get("after") or ""
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

    envs = await _matching_envs(session, project, pushed_branch)
    if not envs:
        return {"ok": True, "ignored": f"no environment tracks branch {pushed_branch}"}

    # Gate on checks only when the repo actually has CI to wait for.
    gate = False
    if project.require_checks:
        token = await _project_token(session, project)
        if token:
            try:
                gate = (await github.count_workflows(token, full_name)) > 0
            except Exception:  # noqa: BLE001 — API hiccup: fall back to instant deploy
                gate = False

    deployed = []
    for env in envs:
        # A newer push supersedes any deployment still waiting on checks.
        stale = (await session.execute(
            select(Deployment).where(
                Deployment.environment_id == env.id,
                Deployment.status.in_(("pending_checks", "pending_promotion", "pending_e2e")),
            )
        )).scalars().all()
        for d in stale:
            d.status = "superseded"

        if env.promotion_gate:
            # Staged pipeline: this env waits for its source env + e2e tests.
            session.add(Deployment(
                environment_id=env.id, status="pending_promotion",
                stack_name=urls.stack_name(project, env),
                commit_sha=sha, trigger="webhook",
            ))
            deployed.append({"environment": env.name, "state": "pending_promotion"})
        elif gate:
            dep = Deployment(
                environment_id=env.id, status="pending_checks",
                stack_name=urls.stack_name(project, env),
                commit_sha=sha, trigger="webhook",
            )
            session.add(dep)
            deployed.append({"environment": env.name, "state": "pending_checks"})
        else:
            dep = await queue_deploy(session, background, env, trigger="webhook")
            deployed.append({"environment": env.name, "deployment_id": dep.id})
    await session.commit()
    return {"ok": True, "deployed": deployed, "gated": gate}


async def _on_workflow_run(session: AsyncSession, background: BackgroundTasks, payload: dict):
    if payload.get("action") != "completed":
        return {"ok": True, "ignored": "not completed"}
    run = payload.get("workflow_run") or {}
    full_name = (payload.get("repository") or {}).get("full_name")
    sha = run.get("head_sha")
    conclusion = run.get("conclusion")
    if not full_name or not sha:
        return {"ok": True, "ignored": "no repo/sha"}

    project = (await session.execute(
        select(Project).where(Project.repo_full_name == full_name)
    )).scalar_one_or_none()
    if not project or not project.managed or not project.auto_deploy:
        return {"ok": True, "ignored": "project not managed / auto-deploy off"}

    # E2E gate: a promotion-gated env waiting on this exact workflow?
    wf_file = (run.get("path") or "").split("/")[-1]
    e2e_result = await _resolve_e2e(session, background, project, sha, wf_file, run, conclusion)

    pending = (await session.execute(
        select(Deployment).join(Environment, Deployment.environment_id == Environment.id)
        .where(
            Environment.project_id == project.id,
            Deployment.status == "pending_checks",
            Deployment.commit_sha == sha,
        )
    )).scalars().all()
    if not pending:
        return {"ok": True, "ignored": "no pending_checks for this commit", **e2e_result}

    if conclusion not in CHECKS_OK:
        for d in pending:
            d.status = "blocked"
            d.error = f"Checks failed: workflow \"{run.get('name')}\" concluded {conclusion}."
        await session.commit()
        return {"ok": True, "blocked": len(pending), **e2e_result}

    # This workflow passed — but promote only when every check run for the
    # commit is done and green (multi-workflow repos emit one event per flow).
    token = await _project_token(session, project)
    if token:
        try:
            checks = await github.list_check_runs(token, full_name, sha)
            if any(c.get("status") != "completed" for c in checks):
                return {"ok": True, "waiting": "other checks still running"}
            bad = [c for c in checks if c.get("conclusion") not in CHECKS_OK]
            if bad:
                for d in pending:
                    d.status = "blocked"
                    d.error = f"Checks failed: {', '.join(c.get('name', '?') for c in bad)}."
                await session.commit()
                return {"ok": True, "blocked": len(pending)}
        except Exception:  # noqa: BLE001 — can't read checks: trust this event's success
            pass

    promoted = []
    for d in pending:
        d.status = "queued"
        d.error = None
        await session.commit()
        background.add_task(engine.run_deploy, d.id, trigger="webhook")
        promoted.append(d.id)
    return {"ok": True, "promoted": promoted, **e2e_result}


async def _resolve_e2e(
    session: AsyncSession, background: BackgroundTasks, project: Project,
    sha: str, wf_file: str, run: dict, conclusion: str | None,
) -> dict:
    """Resolve pending_e2e deployments gated on the workflow that just finished."""
    if not wf_file:
        return {}
    rows = (await session.execute(
        select(Deployment, Environment)
        .join(Environment, Deployment.environment_id == Environment.id)
        .where(
            Environment.project_id == project.id,
            Deployment.status == "pending_e2e",
            Deployment.commit_sha == sha,
        )
    )).all()
    matched = [(d, e) for d, e in rows if (e.e2e_workflow or "") == wf_file]
    if not matched:
        return {}
    out = {"e2e_promoted": [], "e2e_blocked": []}
    for d, e in matched:
        if conclusion in CHECKS_OK:
            d.status = "queued"
            d.error = None
            await session.commit()
            background.add_task(engine.run_deploy, d.id, trigger="webhook")
            out["e2e_promoted"].append(d.id)
        else:
            d.status = "blocked"
            d.error = f"E2E workflow \"{run.get('name')}\" concluded {conclusion}."
            await session.commit()
            out["e2e_blocked"].append(d.id)
    return out
