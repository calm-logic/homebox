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


class LeaveBody(BaseModel):
    stop_tunnel: bool = True
    teardown_stacks: bool = False


@router.post("/leave")
async def leave_cluster(
    body: LeaveBody | None = None,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Leave & disconnect: peers drop their subscriptions to this node (WAL
    slots released), local subscriptions are dropped, the shared-tunnel
    connector stops (unless opted out), and optionally the cluster-enabled
    stacks are torn down. Keys are not rotated."""
    body = body or LeaveBody()
    if not await clusterlib.load_cluster(session):
        raise HTTPException(404, "This node is not part of a cluster.")
    try:
        result = await clusterlib.leave_cluster_flow(
            session, stop_tunnel=body.stop_tunnel, teardown_stacks=body.teardown_stacks,
        )
    except (clusterlib.ControlPlaneError, clusterlib.PeerError) as e:
        raise HTTPException(502, str(e))
    return {"ok": True, **result}


class EvictBody(BaseModel):
    node_id: str


@router.post("/evict")
async def evict(
    body: EvictBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Remove another (typically dead/unreachable) node from the cluster."""
    node_id = await clusterlib.get_node_id(session)
    if body.node_id == node_id:
        raise HTTPException(400, "Use Leave to remove this node.")
    try:
        resp = await clusterlib.evict_node(session, body.node_id)
    except (clusterlib.ControlPlaneError, clusterlib.PeerError) as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "nodes": resp.get("nodes")}


# ───── homebox.sh account (token-less create/join) ────────────────────────────


@router.get("/account")
async def account_status(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    acct = await clusterlib.load_account(session)
    if not acct:
        return {"linked": False}
    overview = await clusterlib._get_setting(session, clusterlib.ACCOUNT_OVERVIEW_KEY)
    return {
        "linked": True,
        "control_plane_url": acct.get("control_plane_url"),
        "node_name": acct.get("node_name"),
        "peer_url": acct.get("peer_url"),
        "linked_at": acct.get("linked_at"),
        "overview": overview if isinstance(overview, dict) else {},
    }


class LinkBody(BaseModel):
    account_token: str
    node_name: str = ""
    peer_url: str
    control_plane_url: str | None = None


@router.post("/account/link")
async def account_link(
    body: LinkBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    peer_url = body.peer_url.strip().rstrip("/")
    if not peer_url.startswith("http"):
        peer_url = f"http://{peer_url}"
    try:
        await clusterlib.link_account_flow(
            session,
            control_plane_url=(body.control_plane_url or settings.homebox_control_plane_url),
            account_token_plain=body.account_token.strip(),
            node_name=body.node_name.strip(),
            peer_url=peer_url,
        )
    except clusterlib.ControlPlaneError as e:
        raise HTTPException(502, str(e))
    return {"ok": True}


@router.delete("/account")
async def account_unlink(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    await clusterlib.unlink_account(session)
    return {"ok": True}


@router.post("/account/refresh")
async def account_refresh(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    try:
        overview = await clusterlib.account_poll(session)
    except clusterlib.ControlPlaneError as e:
        raise HTTPException(502, str(e))
    if overview is None:
        raise HTTPException(404, "Not linked to a homebox.sh account.")
    # Execute a pending invite right away rather than on the next loop tick.
    await clusterlib._maybe_autojoin(session, overview)
    return overview


class AccountCreateBody(BaseModel):
    name: str


@router.post("/account/create-cluster")
async def account_create_cluster(
    body: AccountCreateBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Create a cluster with this node as the seed, using the linked account
    (no token pasting)."""
    if await clusterlib.load_cluster(session):
        raise HTTPException(409, "This node is already in a cluster. Leave it first.")
    acct = await clusterlib.load_account(session)
    if not acct:
        raise HTTPException(404, "Link a homebox.sh account first.")
    from .. import crypto
    try:
        state = await clusterlib.create_cluster_flow(
            session,
            control_plane_url=acct["control_plane_url"],
            account_token_plain=crypto.decrypt(acct["token_encrypted"]),
            name=body.name.strip(),
            peer_url=acct.get("peer_url") or "",
            node_name=acct.get("node_name") or "",
        )
    except clusterlib.ControlPlaneError as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "cluster_id": state["cluster_id"]}


class AccountJoinBody(BaseModel):
    cluster_id: str


@router.post("/account/join")
async def account_join_cluster(
    body: AccountJoinBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Join one of the account's clusters directly — this node mints its own
    join token with the account credential and runs the normal join flow."""
    if await clusterlib.load_cluster(session):
        raise HTTPException(409, "This node is already in a cluster. Leave it first.")
    acct = await clusterlib.load_account(session)
    if not acct:
        raise HTTPException(404, "Link a homebox.sh account first.")
    from .. import crypto
    try:
        minted = await clusterlib._cp(
            "POST", acct["control_plane_url"],
            f"/v1/clusters/{body.cluster_id}/join-tokens",
            token=crypto.decrypt(acct["token_encrypted"]),
        )
        state = await clusterlib.join_cluster_flow(
            session,
            control_plane_url=acct["control_plane_url"],
            join_token=minted["join_token"],
            peer_url=acct.get("peer_url") or "",
            node_name=acct.get("node_name") or "",
        )
    except (clusterlib.ControlPlaneError, clusterlib.PeerError) as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "cluster_id": state["cluster_id"], "restarting": True}


class InviteBody(BaseModel):
    node_id: str
    cluster_id: str | None = None  # default: this node's cluster


@router.post("/account/invite")
async def account_invite(
    body: InviteBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Invite another linked node into a cluster — it joins automatically on
    its next account poll (≤60s)."""
    acct = await clusterlib.load_account(session)
    if not acct:
        raise HTTPException(404, "Link a homebox.sh account first.")
    cluster_id = body.cluster_id
    if not cluster_id:
        state = await clusterlib.load_cluster(session)
        if not state:
            raise HTTPException(400, "Specify cluster_id or join a cluster first.")
        cluster_id = state["cluster_id"]
    from .. import crypto
    try:
        await clusterlib._cp(
            "POST", acct["control_plane_url"], f"/v1/clusters/{cluster_id}/invite",
            token=crypto.decrypt(acct["token_encrypted"]),
            body={"node_id": body.node_id},
        )
    except clusterlib.ControlPlaneError as e:
        raise HTTPException(502, str(e))
    return {"ok": True, "invited": body.node_id, "cluster_id": cluster_id}
