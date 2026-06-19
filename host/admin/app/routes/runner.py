from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import httpx

from ..auth import require_session_api
from ..db import get_session
from ..models import Organization
from ..orgs import decrypted_pat
from ..github import get_org_runner_token, list_org_runners
from ..host import (
    list_runner_containers,
    remove_container,
    restart_container,
    run_runner_container,
)

router = APIRouter(prefix="/api/runner")


@router.get("")
async def runner_summary(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    containers = list_runner_containers()
    orgs = (await session.execute(select(Organization).order_by(Organization.login))).scalars().all()
    org_runners: dict[str, list] = {}
    for o in orgs:
        try:
            data = await list_org_runners(decrypted_pat(o), o.login)
            org_runners[o.login] = data.get("runners", [])
        except httpx.HTTPStatusError:
            org_runners[o.login] = []
    return {
        "containers": containers,
        "org_runners": org_runners,
    }


class InstallBody(BaseModel):
    org: str


@router.post("/install")
async def install_runner(
    body: InstallBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    login = body.org.strip()
    org = (await session.execute(select(Organization).where(Organization.login == login))).scalar_one_or_none()
    if not org:
        raise HTTPException(404, "Organization not connected")

    try:
        token = await get_org_runner_token(decrypted_pat(org), login)
    except httpx.HTTPStatusError as e:
        detail = (e.response.json().get("message") if e.response.headers.get("content-type", "").startswith("application/json") else e.response.text) or ""
        hint = ""
        if e.response.status_code == 403:
            hint = " — the connected token lacks the 'admin:org' scope or you are not an owner of this org. Re-authorize the org, or connect a PAT with 'repo' + 'admin:org'."
        raise HTTPException(400, f"GitHub error fetching registration token: {e.response.status_code} {detail}{hint}")

    container_name = f"homebox-runner-{login.lower()}"
    runner_name = f"homebox-{login.lower()}"
    labels = ["homebox", "self-hosted", f"org:{login}"]

    ok, msg = run_runner_container(
        name=container_name,
        org=login,
        runner_token=token,
        runner_name=runner_name,
        labels=labels,
    )
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True, "container": container_name}


@router.post("/{name}/restart")
async def restart_runner(name: str, user: str = Depends(require_session_api)):
    if not name.startswith("homebox-runner-"):
        raise HTTPException(400, "Refusing to restart non-runner container")
    ok, msg = restart_container(name)
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True}


@router.delete("/{name}")
async def remove_runner(name: str, user: str = Depends(require_session_api)):
    if not name.startswith("homebox-runner-"):
        raise HTTPException(400, "Refusing to remove non-runner container")
    ok, msg = remove_container(name, force=True)
    if not ok:
        raise HTTPException(500, msg)
    return {"ok": True}
