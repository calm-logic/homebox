"""Cloud node provisioning API (linked-accounts D6) — session-authed routes
over app/nodeprovision.py: boot a full Homebox node VM on the user's linked
AWS/GCP account that auto-joins the current cluster, poll its status, and
tear it down. The god view (System page Topology) drives these.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clusterlib, licenselib, nodeprovision
from ..auth import require_session_api
from ..db import get_session

log = logging.getLogger("homebox.provision.api")

router = APIRouter(prefix="/api/cluster/account/nodes")


def _np_http(e: nodeprovision.NodeProvisionError) -> HTTPException:
    return HTTPException(e.status, str(e))


def _cp_http(e: clusterlib.ControlPlaneError) -> HTTPException:
    """Preserve meaningful control-plane statuses (402 plan gating, 503)
    instead of collapsing everything to 502 — same mapping routes/cluster.py
    uses."""
    if e.status_code == 402:
        return HTTPException(402, e.detail)
    if e.status_code == 503:
        return HTTPException(503, e.detail)
    return HTTPException(502, str(e))


class ProvisionBody(BaseModel):
    name: str
    provider: str  # "aws" | "gcp"
    integration_id: int
    region: str
    machine: str | None = None


@router.post("/provision")
async def provision_node(
    body: ProvisionBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    if await clusterlib.load_account(session) is None:
        raise HTTPException(
            412, "Link a homebox.sh account first — cloud nodes are "
                 "provisioned onto your linked cloud account.")
    state = await clusterlib.load_cluster(session)
    if not state:
        raise HTTPException(
            412, "This node is not part of a cluster — the new VM needs a "
                 "cluster to join. Create one first.")
    status = licenselib.license_status(state)
    if "cluster" not in (status.get("features") or []):
        raise HTTPException(
            402, "Adding cloud nodes requires a plan with the cluster "
                 "feature — upgrade at homebox.sh.")
    try:
        entry = await nodeprovision.provision_node(
            session,
            name=body.name,
            provider=body.provider,
            integration_id=body.integration_id,
            region=body.region,
            machine=body.machine,
        )
    except nodeprovision.NodeProvisionError as e:
        raise _np_http(e)
    except clusterlib.ControlPlaneError as e:
        raise _cp_http(e)
    return entry


@router.get("/provision")
async def list_provisions(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    return {"provisions": await nodeprovision.refresh_provisions(session)}


@router.delete("/provision/{provision_id}")
async def teardown_provision(
    provision_id: str,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    try:
        entry = await nodeprovision.teardown_provision(session, provision_id)
    except nodeprovision.NodeProvisionError as e:
        raise _np_http(e)
    return {"ok": True, "removed": entry}
