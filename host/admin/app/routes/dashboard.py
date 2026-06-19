from fastapi import APIRouter, Depends
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_session_api
from ..db import get_session
from ..models import Organization, Repository, Domain
from ..host import list_runner_containers, runner_status

router = APIRouter(prefix="/api")


@router.get("/summary")
async def summary(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    org_count = (await session.execute(select(func.count()).select_from(Organization))).scalar_one()
    repo_count = (await session.execute(select(func.count()).select_from(Repository))).scalar_one()
    domain_count = (await session.execute(select(func.count()).select_from(Domain))).scalar_one()
    runners = list_runner_containers()
    host = runner_status()
    return {
        "org_count": org_count,
        "repo_count": repo_count,
        "domain_count": domain_count,
        "runner": {
            "installed": host.get("installed", False) or len(runners) > 0,
            "container_count": len(runners),
        },
    }
