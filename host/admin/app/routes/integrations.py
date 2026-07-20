"""Integrations API — connections to external systems (GitHub / GitLab /
Cloudflare / AWS / GCP). Lists every Integration row, and handles GitHub
connect-via-PAT, AWS connect-via-access-keys, GCP connect-via-service-account,
repo sync, and disconnect. GitHub OAuth connect lives in routes/oauth.py;
Cloudflare connect/disconnect lives in routes/tunnel.py — both write Integration
rows that show up here.
"""

import json
from datetime import datetime

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
from ..models import Integration, Project, ServiceTarget
from ..targets.awslib import AwsClient, AwsError
from ..targets.gcplib import GcpClient, GcpError
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
        "source": "keys" if i.provider == "aws"
        else "service-account" if i.provider == "gcp"
        else "oauth" if (i.secret_encrypted or "").startswith("oauth:") else "pat"
        if i.provider != "cloudflare" else "token",
        # Account-scoped github rows cover the identity's own repos + granted
        # orgs; legacy rows are one org each ("org").
        "scope": (i.config or {}).get("scope") or ("org" if i.provider == "github" else None),
        "orgs": (i.config or {}).get("orgs") or [],
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


class AwsConnectBody(BaseModel):
    access_key_id: str
    secret_access_key: str
    region: str = "us-east-1"


@router.post("/aws/connect")
async def connect_aws(
    body: AwsConnectBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    key_id = body.access_key_id.strip()
    secret = body.secret_access_key.strip()
    region = (body.region or "").strip() or "us-east-1"
    if not key_id or not secret:
        raise HTTPException(400, "Access key ID and secret access key are required")

    try:
        ident = await AwsClient(key_id, secret, region).sts_get_caller_identity()
    except AwsError as e:
        raise HTTPException(400, f"AWS rejected the credentials: {e.message or e}")
    account = ident.get("account")
    if not account:
        raise HTTPException(400, "AWS did not return an account id for these credentials")

    existing = (await session.execute(
        select(Integration).where(Integration.provider == "aws", Integration.account_login == account)
    )).scalar_one_or_none()
    row = existing or Integration(provider="aws", account_login=account)
    row.secret_encrypted = encrypt(f"{key_id}:{secret}")
    row.account_id = account
    row.name = f"AWS {account}"
    row.config = {"region": region, "arn": ident.get("arn")}
    row.status = "connected"
    row.updated_at = datetime.utcnow()
    if not existing:
        session.add(row)
    await session.commit()
    return _serialize(row)


class GcpConnectBody(BaseModel):
    service_account_json: str
    region: str = "us-central1"


@router.post("/gcp/connect")
async def connect_gcp(
    body: GcpConnectBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    raw = body.service_account_json.strip()
    region = (body.region or "").strip() or "us-central1"
    if not raw:
        raise HTTPException(400, "Service-account key JSON is required")
    try:
        sa = json.loads(raw)
    except ValueError:
        raise HTTPException(400, "Service-account key is not valid JSON")
    if not isinstance(sa, dict):
        raise HTTPException(400, "Service-account key must be a JSON object")
    missing = [k for k in ("client_email", "private_key", "project_id") if not sa.get(k)]
    if missing:
        raise HTTPException(400, f"Service-account key is missing {', '.join(missing)}")

    try:
        project = await GcpClient(sa).get_project()
    except GcpError as e:
        raise HTTPException(400, f"GCP rejected the service account: {e}")

    project_id = sa["project_id"]
    existing = (await session.execute(
        select(Integration).where(Integration.provider == "gcp", Integration.account_login == project_id)
    )).scalar_one_or_none()
    row = existing or Integration(provider="gcp", account_login=project_id)
    row.secret_encrypted = encrypt(raw)
    row.account_id = str(project.get("projectNumber") or "") or None
    row.name = f"GCP {project_id}"
    row.config = {"region": region, "client_email": sa["client_email"]}
    row.status = "connected"
    row.updated_at = datetime.utcnow()
    if not existing:
        session.add(row)
    await session.commit()
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

    # Cloud accounts can't be disconnected while services still deploy through
    # them — the credentials are needed to tear the cloud resources down.
    if integ.provider in ("aws", "gcp", "cloudflare"):
        referenced = (await session.execute(
            select(ServiceTarget.id).where(ServiceTarget.integration_id == integ.id).limit(1)
        )).first()
        if referenced:
            raise HTTPException(
                409, "targets still deployed through this account — retarget them to Homebox first"
            )

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
        project.updated_at = datetime.utcnow()  # cluster sync: newer-wins config edit
        await session.flush()
        await sync_project_webhook(session, project)

    # Record tombstones for everything this delete cascades away (the
    # integration and every project under it, plus those projects' services and
    # environments) so peers/mirrors converge on the removal.
    from .. import cluster_sync
    from ..models import Environment, Service
    all_projects = (await session.execute(
        select(Project).where(Project.integration_id == integ.id)
    )).scalars().all()
    tombs: list[tuple[str, object]] = [
        ("integration", [integ.provider, integ.account_login]),
    ]
    for project in all_projects:
        tombs.append(("project", project.repo_full_name))
        for svc in (await session.execute(
            select(Service).where(Service.project_id == project.id)
        )).scalars():
            tombs.append(("service", [project.name, svc.name]))
        for env in (await session.execute(
            select(Environment).where(Environment.project_id == project.id)
        )).scalars():
            tombs.append(("environment", [project.name, env.name]))
    await cluster_sync.record_tombstones(session, tombs, commit=False)

    await session.delete(integ)  # cascades to projects -> environments/services
    await session.commit()
    return {"ok": True}
