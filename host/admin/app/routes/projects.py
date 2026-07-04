"""Projects API — the unit a user adopts and Homebox deploys.

A Project is 1:1 with a repo (created managed=False on integration sync). Adopting
it (managed=True) auto-creates its production + dev Environments and dissects the
repo into Services with auto-wired connection env vars. Deploys target a
(project, environment) and are tracked as Deployment + ServiceInstance rows.
"""

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import deploy as engine
from .. import dissect, urls
from ..auth import require_session_api
from ..db import get_session
from ..integrations_lib import SLUG_RE, decrypted_token, slugify
from ..models import (
    Deployment, Domain, Environment, Integration, Project, Service, ServiceEnvVar,
    ServiceInstance, WorkflowRunCache,
)
from ..webhooks_lib import sync_project_webhook

router = APIRouter(prefix="/api/projects")

# Environments created when a project is adopted. Production gets the bare
# hostname; dev gets the --dev suffix. Branch defaults to the repo default.
_DEFAULT_ENVS = [
    {"name": "production", "kind": "production", "slug_suffix": "", "is_default": True},
    {"name": "dev", "kind": "dev", "slug_suffix": "--dev", "is_default": False},
]


# ───── helpers ────────────────────────────────────────────────────────────────

async def _get_project(session: AsyncSession, project_id: int) -> Project:
    project = await session.get(Project, project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


async def _project_envs(session: AsyncSession, project_id: int) -> list[Environment]:
    return list((await session.execute(
        select(Environment).where(Environment.project_id == project_id).order_by(Environment.id)
    )).scalars().all())


async def _project_services(session: AsyncSession, project_id: int) -> list[Service]:
    return list((await session.execute(
        select(Service).where(Service.project_id == project_id).order_by(Service.id)
    )).scalars().all())


async def _latest_deployment(session: AsyncSession, env_id: int) -> Deployment | None:
    return (await session.execute(
        select(Deployment).where(Deployment.environment_id == env_id)
        .order_by(Deployment.created_at.desc()).limit(1)
    )).scalar_one_or_none()


async def ensure_environments(session: AsyncSession, project: Project) -> None:
    existing = {e.name for e in await _project_envs(session, project.id)}
    for spec in _DEFAULT_ENVS:
        if spec["name"] in existing:
            continue
        branch = project.default_branch if spec["kind"] == "production" else None
        session.add(Environment(project_id=project.id, branch=branch, **spec))


async def dissect_project(session: AsyncSession, project: Project) -> list[dissect.DetectedService]:
    """Clone the production branch, dissect it, and upsert Service + auto
    ServiceEnvVar rows. Caller has committed env creation first."""
    integration = await session.get(Integration, project.integration_id) if project.integration_id else None
    if not integration:
        raise HTTPException(400, "Project has no source-control integration.")
    prod = next((e for e in await _project_envs(session, project.id) if e.name == "production"), None)
    if not prod:
        raise HTTPException(400, "Project has no production environment.")

    token = decrypted_token(integration)
    await engine.sync_source(project, prod, token)
    rd = engine.repo_dir(project.name, prod.name)
    detected = dissect.dissect(rd)

    existing = {s.name: s for s in await _project_services(session, project.id)}
    seen: set[str] = set()
    for d in detected:
        seen.add(d.name)
        svc = existing.get(d.name)
        if svc is None:
            svc = Service(project_id=project.id, name=d.name)
            session.add(svc)
            await session.flush()
        svc.kind = d.kind
        # source_type = how it's built; source_ref = build dir (for display).
        svc.source_type = d.build_type or d.origin
        svc.source_ref = d.build_dir
        svc.is_public = d.is_public
        svc.subdomain_label = d.subdomain_label
        svc.internal_port = d.internal_port
        svc.depends_on = d.depends_on
        svc.env_template = d.env_template
        # Replace auto-wired env vars; leave user-set ones untouched.
        await session.execute(
            delete(ServiceEnvVar).where(
                ServiceEnvVar.service_id == svc.id, ServiceEnvVar.source == "auto"
            )
        )
        for k, v in d.auto_env.items():
            session.add(ServiceEnvVar(service_id=svc.id, key=k, value=v, source="auto"))
    for name, svc in existing.items():
        if name not in seen:
            await session.delete(svc)

    project.detected_stack = {
        "services": [
            {"name": d.name, "kind": d.kind, "public": d.is_public, "label": d.subdomain_label}
            for d in detected
        ]
    }
    project.dissected_at = datetime.utcnow()
    await session.commit()
    return detected


async def queue_deploy(session: AsyncSession, background: BackgroundTasks,
                       env: Environment, *, trigger: str) -> Deployment:
    """Create a queued Deployment for an environment and schedule run_deploy."""
    project = await session.get(Project, env.project_id)
    dep = Deployment(
        environment_id=env.id,
        status="queued",
        stack_name=urls.stack_name(project, env),
        trigger=trigger,
    )
    session.add(dep)
    await session.commit()
    await session.refresh(dep)
    background.add_task(engine.run_deploy, dep.id, trigger=trigger)
    return dep


# ───── serialization ──────────────────────────────────────────────────────────

def _dep_summary(d: Deployment | None) -> dict | None:
    if not d:
        return None
    return {
        "id": d.id, "status": d.status, "commit_sha": d.commit_sha,
        "error": d.error, "trigger": d.trigger,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


async def _serialize_env(session: AsyncSession, env: Environment) -> dict:
    dep = await _latest_deployment(session, env.id)
    instances = []
    if dep:
        instances = [
            {"service_name": i.service_name, "url": i.url, "status": i.status,
             "container_name": i.container_name}
            for i in (await session.execute(
                select(ServiceInstance).where(ServiceInstance.deployment_id == dep.id)
            )).scalars().all()
        ]
    return {
        "id": env.id, "name": env.name, "kind": env.kind, "branch": env.branch,
        "slug_suffix": env.slug_suffix, "is_default": env.is_default,
        "domain_id": env.domain_id,
        "promotion_gate": env.promotion_gate,
        "e2e_workflow": env.e2e_workflow,
        "promote_from_env_id": env.promote_from_env_id,
        "deployment": _dep_summary(dep),
        "instances": instances,
    }


def _serialize_service(s: Service, env_vars: list[ServiceEnvVar]) -> dict:
    return {
        "id": s.id, "name": s.name, "kind": s.kind, "source_type": s.source_type,
        "source_ref": s.source_ref, "is_public": s.is_public,
        "subdomain_label": s.subdomain_label, "internal_port": s.internal_port,
        "depends_on": s.depends_on or [], "env_template": s.env_template or {},
        "env_vars": [
            {"id": v.id, "key": v.key, "value": ("••••••" if v.is_secret else v.value),
             "source": v.source, "is_secret": v.is_secret, "environment_id": v.environment_id}
            for v in env_vars if v.service_id == s.id
        ],
    }


def _serialize_project(p: Project, integration: Integration | None, domain: Domain | None,
                       envs_summary: list[dict] | None = None) -> dict:
    return {
        "id": p.id,
        "repo_full_name": p.repo_full_name,
        "name": p.name,
        "default_branch": p.default_branch,
        "managed": p.managed,
        "auto_deploy": p.auto_deploy,
        "require_checks": p.require_checks,
        "domain_id": p.domain_id,
        "domain": domain.name if domain else None,
        "description": p.description,
        "dissected_at": p.dissected_at.isoformat() if p.dissected_at else None,
        "detected_stack": p.detected_stack or {},
        "integration": {
            "id": integration.id, "provider": integration.provider,
            "account_login": integration.account_login,
        } if integration else None,
        "environments": envs_summary if envs_summary is not None else [],
    }


# ───── list ───────────────────────────────────────────────────────────────────

@router.get("")
async def list_projects(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    projects = (await session.execute(select(Project).order_by(Project.repo_full_name))).scalars().all()
    integrations = {i.id: i for i in (await session.execute(select(Integration))).scalars().all()}
    domains = {d.id: d for d in (await session.execute(select(Domain))).scalars().all()}
    out = []
    for p in projects:
        envs = []
        if p.managed:
            envs = [await _serialize_env(session, e) for e in await _project_envs(session, p.id)]
        out.append(_serialize_project(
            p, integrations.get(p.integration_id), domains.get(p.domain_id), envs
        ))
    return out


@router.get("/{project_id}")
async def get_project(
    project_id: int,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    p = await _get_project(session, project_id)
    integration = await session.get(Integration, p.integration_id) if p.integration_id else None
    domain = await session.get(Domain, p.domain_id) if p.domain_id else None
    envs = [await _serialize_env(session, e) for e in await _project_envs(session, p.id)]
    services = await _project_services(session, p.id)
    all_vars = (await session.execute(
        select(ServiceEnvVar).join(Service, ServiceEnvVar.service_id == Service.id)
        .where(Service.project_id == p.id)
    )).scalars().all()
    result = _serialize_project(p, integration, domain, envs)
    result["services"] = [_serialize_service(s, all_vars) for s in services]
    return result


# ───── adopt / release / patch / sync ─────────────────────────────────────────

class AdoptBody(BaseModel):
    name: str | None = None
    domain_id: int | None = None


@router.post("/{project_id}/adopt")
async def adopt_project(
    project_id: int,
    body: AdoptBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    p = await _get_project(session, project_id)
    if body.name:
        slug = slugify(body.name)
        if not SLUG_RE.match(slug):
            raise HTTPException(400, "Name must be lowercase letters, numbers, and hyphens (1–63 chars).")
        clash = (await session.execute(
            select(Project).where(Project.name == slug, Project.id != p.id)
        )).scalar_one_or_none()
        if clash:
            raise HTTPException(409, f"Name '{slug}' is already used by {clash.repo_full_name}.")
        p.name = slug
    if body.domain_id is not None:
        if body.domain_id and not await session.get(Domain, body.domain_id):
            raise HTTPException(404, "Domain not found.")
        p.domain_id = body.domain_id or None

    p.managed = True
    await ensure_environments(session, p)
    await session.commit()

    note = None
    try:
        await dissect_project(session, p)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001 — adoption shouldn't fail on a bad repo
        note = f"Adopted, but could not dissect the repo yet: {e}"

    _ok, webhook_note = await sync_project_webhook(session, p)
    return {"ok": True, "id": p.id, "name": p.name, "note": note, "webhook_note": webhook_note}


@router.post("/{project_id}/release")
async def release_project(
    project_id: int,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    p = await _get_project(session, project_id)
    for env in await _project_envs(session, p.id):
        await engine.teardown_stack(p.name, env.name)
    p.managed = False
    await session.commit()
    await sync_project_webhook(session, p)  # best-effort hook removal
    return {"ok": True, "id": p.id}


class PatchBody(BaseModel):
    name: str | None = None
    domain_id: int | None = None
    auto_deploy: bool | None = None
    require_checks: bool | None = None


@router.patch("/{project_id}")
async def patch_project(
    project_id: int,
    body: PatchBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    p = await _get_project(session, project_id)
    if body.name is not None:
        slug = slugify(body.name)
        if not SLUG_RE.match(slug):
            raise HTTPException(400, "Name must be lowercase letters, numbers, and hyphens.")
        clash = (await session.execute(
            select(Project).where(Project.name == slug, Project.id != p.id)
        )).scalar_one_or_none()
        if clash:
            raise HTTPException(409, f"Name '{slug}' is already used by {clash.repo_full_name}.")
        p.name = slug
    if body.domain_id is not None:
        if body.domain_id and not await session.get(Domain, body.domain_id):
            raise HTTPException(404, "Domain not found.")
        p.domain_id = body.domain_id or None
    if body.auto_deploy is not None:
        p.auto_deploy = body.auto_deploy
    if body.require_checks is not None:
        p.require_checks = body.require_checks
    await session.commit()
    if body.auto_deploy is not None:
        await sync_project_webhook(session, p)
    return {"ok": True, "id": p.id, "name": p.name}


@router.post("/{project_id}/sync")
async def sync_project(
    project_id: int,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Re-dissect the repo: refresh Services + auto-wired env vars."""
    p = await _get_project(session, project_id)
    if not p.managed:
        raise HTTPException(400, "Adopt the project before syncing its services.")
    await ensure_environments(session, p)
    await session.commit()
    detected = await dissect_project(session, p)
    return {"ok": True, "services": len(detected)}


# ───── deploy / stop per environment ──────────────────────────────────────────

async def _get_env(session: AsyncSession, project_id: int, env_id: int) -> Environment:
    env = await session.get(Environment, env_id)
    if not env or env.project_id != project_id:
        raise HTTPException(404, "Environment not found")
    return env


@router.post("/{project_id}/environments/{env_id}/deploy")
async def deploy_environment(
    project_id: int,
    env_id: int,
    background: BackgroundTasks,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    p = await _get_project(session, project_id)
    if not p.managed:
        raise HTTPException(400, "Adopt the project before deploying.")
    env = await _get_env(session, project_id, env_id)
    dep = await queue_deploy(session, background, env, trigger="manual")
    return {"id": dep.id, "status": dep.status, "environment": env.name}


@router.get("/{project_id}/workflows")
async def project_workflows(
    project_id: int,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    p = await _get_project(session, project_id)
    rows = (await session.execute(
        select(WorkflowRunCache)
        .where(WorkflowRunCache.repository_full_name == p.repo_full_name)
        .order_by(WorkflowRunCache.created_at.desc()).limit(30)
    )).scalars().all()
    return [{
        "id": r.id, "run_id": r.run_id, "name": r.name, "status": r.status,
        "conclusion": r.conclusion, "head_branch": r.head_branch,
        "html_url": r.html_url,
        "created_at": r.created_at.isoformat() if r.created_at else None,
    } for r in rows]


@router.get("/{project_id}/environments/{env_id}/deployments")
async def environment_deployments(
    project_id: int,
    env_id: int,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Recent deploy history for one environment — Homebox's own runs, not
    GitHub Actions (those are /workflows)."""
    await _get_project(session, project_id)
    env = await _get_env(session, project_id, env_id)
    rows = (await session.execute(
        select(Deployment).where(Deployment.environment_id == env.id)
        .order_by(Deployment.created_at.desc()).limit(20)
    )).scalars().all()
    return [{
        "id": d.id, "status": d.status, "commit_sha": d.commit_sha,
        "trigger": d.trigger, "error": d.error,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    } for d in rows]


class PatchEnvBody(BaseModel):
    domain_id: int | None = None   # 0 clears the override (inherit project)
    branch: str | None = None      # "" clears (track default branch)
    promotion_gate: bool | None = None
    e2e_workflow: str | None = None      # "" clears
    promote_from_env_id: int | None = None  # 0 clears (auto: the dev env)


@router.patch("/{project_id}/environments/{env_id}")
async def patch_environment(
    project_id: int,
    env_id: int,
    body: PatchEnvBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Environment-specific overrides of project settings (domain, branch)."""
    await _get_project(session, project_id)
    env = await _get_env(session, project_id, env_id)
    if body.domain_id is not None:
        env.domain_id = body.domain_id or None
    if body.branch is not None:
        env.branch = body.branch.strip() or None
    if body.promotion_gate is not None:
        env.promotion_gate = body.promotion_gate
    if body.e2e_workflow is not None:
        env.e2e_workflow = body.e2e_workflow.strip() or None
    if body.promote_from_env_id is not None:
        env.promote_from_env_id = body.promote_from_env_id or None
    await session.commit()
    return {"ok": True, "id": env.id, "domain_id": env.domain_id, "branch": env.branch,
            "promotion_gate": env.promotion_gate, "e2e_workflow": env.e2e_workflow}


@router.get("/{project_id}/deployments/{deployment_id}")
async def deployment_detail(
    project_id: int,
    deployment_id: int,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """One deployment with its full log — polled by the UI while it's active."""
    p = await _get_project(session, project_id)
    dep = await session.get(Deployment, deployment_id)
    env = await session.get(Environment, dep.environment_id) if dep else None
    if not dep or not env or env.project_id != p.id:
        raise HTTPException(404, "Deployment not found")
    return {
        "id": dep.id, "status": dep.status, "commit_sha": dep.commit_sha,
        "trigger": dep.trigger, "error": dep.error, "log_tail": dep.log_tail,
        "stack_name": dep.stack_name,
        "environment": {"id": env.id, "name": env.name},
        "created_at": dep.created_at.isoformat() if dep.created_at else None,
        "updated_at": dep.updated_at.isoformat() if dep.updated_at else None,
    }


@router.post("/{project_id}/environments/{env_id}/stop")
async def stop_environment(
    project_id: int,
    env_id: int,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    p = await _get_project(session, project_id)
    env = await _get_env(session, project_id, env_id)
    ok, msg = await engine.teardown_stack(p.name, env.name)
    dep = await _latest_deployment(session, env.id)
    if dep:
        dep.status = "stopped"
        await session.commit()
    return {"ok": ok, "message": msg}
