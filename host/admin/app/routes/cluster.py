"""Cluster UI API (session-authed) — create/join/status/leave + join-token mint.

The peer-to-peer machinery lives in routes/peer.py + clusterlib; these are the
endpoints the Cluster page calls into.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clusterlib
from ..auth import require_session_api
from ..config import settings
from ..db import get_session

log = logging.getLogger("homebox.cluster.api")

router = APIRouter(prefix="/api/cluster")


@router.get("")
async def cluster_status(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    node_id = await clusterlib.get_node_id(session)
    state = await clusterlib.load_cluster(session)
    if not state:
        return {
            "active": False,
            "node_id": node_id,
            "control_plane_url": settings.homebox_control_plane_url,
        }
    return {
        "active": True,
        "node_id": node_id,
        "cluster_id": state["cluster_id"],
        "name": state.get("name"),
        "node_name": state.get("node_name"),
        "peer_url": state.get("peer_url"),
        "control_plane_url": state.get("control_plane_url"),
        "roster": state.get("roster") or [],
        "license": state.get("license"),
        "initial_sync_done": bool(state.get("initial_sync_done")),
        "last_heartbeat": state.get("last_heartbeat"),
        "last_sync_at": state.get("last_sync_at"),
        "joined_at": state.get("joined_at"),
    }


class CreateBody(BaseModel):
    name: str
    account_token: str
    peer_url: str
    node_name: str = ""
    control_plane_url: str | None = None


@router.post("/create")
async def create_cluster(
    body: CreateBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    if await clusterlib.load_cluster(session):
        raise HTTPException(409, "This node is already in a cluster. Leave it first.")
    peer_url = body.peer_url.strip().rstrip("/")
    if not peer_url.startswith("http"):
        peer_url = f"http://{peer_url}"
    try:
        state = await clusterlib.create_cluster_flow(
            session,
            control_plane_url=(body.control_plane_url or settings.homebox_control_plane_url),
            account_token_plain=body.account_token.strip(),
            name=body.name.strip(),
            peer_url=peer_url,
            node_name=body.node_name.strip(),
        )
    except clusterlib.ControlPlaneError as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "cluster_id": state["cluster_id"]}


class JoinBody(BaseModel):
    join_token: str
    peer_url: str
    node_name: str = ""
    control_plane_url: str | None = None


@router.post("/join")
async def join_cluster(
    body: JoinBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    if await clusterlib.load_cluster(session):
        raise HTTPException(409, "This node is already in a cluster. Leave it first.")
    peer_url = body.peer_url.strip().rstrip("/")
    if not peer_url.startswith("http"):
        peer_url = f"http://{peer_url}"
    try:
        state = await clusterlib.join_cluster_flow(
            session,
            control_plane_url=(body.control_plane_url or settings.homebox_control_plane_url),
            join_token=body.join_token,
            peer_url=peer_url,
            node_name=body.node_name.strip(),
        )
    except (clusterlib.ControlPlaneError, clusterlib.PeerError) as e:
        raise HTTPException(502, str(e))
    # The admin restarts in ~2s to adopt the cluster keys; the UI should poll
    # /api/cluster until it comes back with active=true + initial_sync_done.
    return {"ok": True, "cluster_id": state["cluster_id"], "restarting": True}


@router.post("/join-token")
async def mint_join_token(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    state = await clusterlib.load_cluster(session)
    if not state:
        raise HTTPException(404, "This node is not part of a cluster.")
    acct = clusterlib.account_token(state)
    if not acct:
        raise HTTPException(400, "No account token on this node — mint the join token from the founding node.")
    try:
        resp = await clusterlib._cp(
            "POST", state["control_plane_url"],
            f"/v1/clusters/{state['cluster_id']}/join-tokens",
            token=acct,
        )
    except clusterlib.ControlPlaneError as e:
        raise HTTPException(502, str(e))
    return resp


@router.post("/sync")
async def sync_now(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    state = await clusterlib.load_cluster(session)
    if not state:
        raise HTTPException(404, "This node is not part of a cluster.")
    if not state.get("initial_sync_done"):
        ok = await clusterlib.initial_sync(session, state)
        return {"ok": ok, "initial": True}
    await clusterlib.reconcile_from_peers(session, state)
    return {"ok": True}


@router.post("/leave")
async def leave_cluster(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Leave the cluster: best-effort deregister at the control plane, then
    drop local membership. Does NOT tear down running stacks or rotate the
    (formerly shared) keys — that's a deliberate non-destructive default."""
    state = await clusterlib.load_cluster(session)
    if not state:
        raise HTTPException(404, "This node is not part of a cluster.")
    node_id = await clusterlib.get_node_id(session)
    acct = clusterlib.account_token(state)
    if acct:
        try:
            await clusterlib._cp(
                "DELETE", state["control_plane_url"],
                f"/v1/clusters/{state['cluster_id']}/nodes/{node_id}",
                token=acct,
            )
        except clusterlib.ControlPlaneError as e:
            log.warning("leave: control-plane deregister failed: %s", e)
    await clusterlib.clear_cluster(session)
    return {"ok": True}
