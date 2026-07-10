"""Intra-cluster peer API — node-to-node, never session-authed.

Reached through each node's Traefik on :80 with Host `homebox-peer.internal`
(a file-provider route write_traefik_dynamic always emits), so no extra ports
are published. Two auth tiers:

  handshake   grant-authed: the joining node presents the signed grant it got
              from the control plane; we verify it THERE (with our node token)
              and get back the registered pubkey to seal the cluster keys to.
  everything  HMAC bearer derived from the shared cluster secret
  else        (clusterlib.verify_peer_token), caller must be in the roster.
"""

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import cluster_sync, clusterlib
from ..config import settings
from ..db import get_session
from ..models import Deployment, Environment, Project

log = logging.getLogger("homebox.cluster.peer")

router = APIRouter(prefix="/peer")


async def _cluster_state(session: AsyncSession) -> dict[str, Any]:
    state = await clusterlib.load_cluster(session)
    if not state:
        raise HTTPException(404, "This node is not part of a cluster.")
    return state


async def require_peer(
    request: Request, session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Verify the HMAC bearer + roster membership. Returns {state, caller_id}."""
    state = await _cluster_state(session)
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else ""
    caller = clusterlib.verify_peer_token(clusterlib.cluster_secret(state), token)
    if not caller:
        raise HTTPException(401, "Invalid peer token")
    roster_ids = {n.get("node_id") for n in state.get("roster") or []}
    if caller not in roster_ids:
        raise HTTPException(403, "Caller is not in the cluster roster")
    return {"state": state, "caller_id": caller}


@router.get("/ping")
async def ping(peer: dict = Depends(require_peer), session: AsyncSession = Depends(get_session)):
    return {
        "node_id": await clusterlib.get_node_id(session),
        "cluster_id": peer["state"]["cluster_id"],
        "version": clusterlib.VERSION,
        # Live serving state, so a peer's last-serving-node guard sees the truth
        # instead of the lagging roster.
        "serving": await clusterlib.get_app_serving(session),
        # This node's cluster role, so peers can distinguish a standby mirror.
        "role": settings.node_role,
    }


class HandshakeBody(BaseModel):
    cluster_id: str
    node_id: str
    pubkey: str
    grant: str


@router.post("/handshake")
async def handshake(body: HandshakeBody, session: AsyncSession = Depends(get_session)):
    """A newly registered node asks for the cluster keys. We confirm its grant
    with the control plane, check the pubkey it presents is the one it
    registered, then seal {encryption_key, app_secret, cluster_secret,
    account_token} to that key. The control plane never sees the payload."""
    state = await _cluster_state(session)
    if state["cluster_id"] != body.cluster_id:
        raise HTTPException(403, "Wrong cluster")
    try:
        verdict = await clusterlib._cp(
            "POST", state["control_plane_url"],
            f"/v1/clusters/{state['cluster_id']}/grants/verify",
            token=clusterlib.node_token(state),
            body={"grant": body.grant},
        )
    except clusterlib.ControlPlaneError as e:
        raise HTTPException(502, f"Could not verify the join grant: {e}")
    if verdict.get("node_id") != body.node_id or verdict.get("pubkey") != body.pubkey:
        raise HTTPException(403, "Grant does not match the presented node identity")

    from .. import crypto
    sealed = crypto.seal_to(body.pubkey, {
        "encryption_key": settings.encryption_key,
        "app_secret": settings.app_secret,
        "cluster_secret": clusterlib.cluster_secret(state),
        "account_token": clusterlib.account_token(state),
    })
    log.info("handshake: sealed cluster keys for joining node %s", body.node_id)
    return {"sealed": sealed}


@router.get("/state")
async def peer_state(peer: dict = Depends(require_peer), session: AsyncSession = Depends(get_session)):
    node_id = await clusterlib.get_node_id(session)
    return await cluster_sync.export_state(session, node_id)


class NodeLeavingBody(BaseModel):
    node_id: str


@router.post("/node-leaving")
async def node_leaving(
    body: NodeLeavingBody,
    peer: dict = Depends(require_peer),
    session: AsyncSession = Depends(get_session),
):
    """A peer is disconnecting: drop OUR subscriptions to it now (this is what
    releases its WAL slots on our side) instead of waiting for the roster to
    shrink and the reconcile loop to notice."""
    from .. import cluster_db
    from ..deploy import repo_dir
    from ..urls import stack_name as make_stack_name
    if body.node_id != peer["caller_id"]:
        raise HTTPException(403, "Nodes can only announce their own departure")
    state = peer["state"]
    ordinal = cluster_db.node_ordinal(state, body.node_id)
    dropped: list[str] = []
    rows = (await session.execute(
        select(Deployment, Environment, Project)
        .join(Environment, Deployment.environment_id == Environment.id)
        .join(Project, Environment.project_id == Project.id)
        .where(Deployment.status == "running")
    )).all()
    seen: set[str] = set()
    for dep, env, project in rows:
        stack = make_stack_name(project, env)
        if stack in seen:
            continue
        seen.add(stack)
        rd = repo_dir(project.name, env.name)
        if not cluster_db.cluster_db_enabled(rd):
            continue
        for info in cluster_db.infos_from_compose(rd):
            dropped += await cluster_db.drop_subscriptions(
                stack=stack, info=info, to_ordinal=ordinal,
            )
    log.info("peer %s leaving: dropped %s", body.node_id, dropped)
    return {"ok": True, "subs_dropped": dropped}


class SetServingBody(BaseModel):
    serving: bool


@router.post("/set-serving")
async def set_serving(
    body: SetServingBody,
    peer: dict = Depends(require_peer),
    session: AsyncSession = Depends(get_session),
):
    """A peer (driven from the cluster UI) drains or resumes app traffic on this
    node: the Cloudflare connector goes down/up so the shared tunnel routes app
    requests to healthy peers, while this node's admin + cluster loop keep
    running — so it stays reachable on the LAN, keeps heartbeating, and can be
    re-enabled at any time."""
    if not body.serving:
        self_id = await clusterlib.get_node_id(session)
        if not await clusterlib.serving_peers_excluding(session, peer["state"], self_id):
            # Allowed when an online mirror is standing by to auto-promote.
            if not await clusterlib.online_mirror_standby(session, peer["state"], self_id):
                raise HTTPException(409, "Refusing to drain the last serving node in the cluster.")
    result = await clusterlib.apply_app_serving(session, body.serving)
    log.info("peer %s set serving=%s → %s", peer["caller_id"], body.serving, result)
    return {"ok": True, **result}


class PeerDeployBody(BaseModel):
    project_name: str
    env_name: str
    commit_sha: str | None = None
    source_peer_url: str = ""
    # Redeploy even at the same commit — set when the PLATFORM changed the
    # stack (e.g. a single-node → cluster DB transition), which the sha-based
    # dedupe below can't see.
    force: bool = False


@router.post("/deploy")
async def peer_deploy(
    body: PeerDeployBody,
    peer: dict = Depends(require_peer),
    session: AsyncSession = Depends(get_session),
):
    """A peer successfully deployed (project, env) and wants us to match it.
    Pull its current config first (mode=deploy → env-var/domain edits ride
    along), then queue the local deploy."""
    state = peer["state"]
    node_id = await clusterlib.get_node_id(session)
    if body.source_peer_url:
        try:
            export = await clusterlib.peer_request(
                "GET", body.source_peer_url, "/peer/state",
                secret=clusterlib.cluster_secret(state), self_node_id=node_id,
            )
            await cluster_sync.import_state(session, export, mode="deploy")
        except clusterlib.PeerError as e:
            log.warning("peer deploy: config pull from %s failed, deploying with local config: %s",
                        body.source_peer_url, e)

    project = (await session.execute(
        select(Project).where(Project.name == body.project_name)
    )).scalar_one_or_none()
    if not project or not project.managed:
        raise HTTPException(404, f"Unknown or unmanaged project {body.project_name!r}")
    env = (await session.execute(
        select(Environment).where(
            Environment.project_id == project.id, Environment.name == body.env_name,
        )
    )).scalar_one_or_none()
    if not env:
        raise HTTPException(404, f"Unknown environment {body.env_name!r}")

    latest = (await session.execute(
        select(Deployment).where(Deployment.environment_id == env.id)
        .order_by(Deployment.created_at.desc()).limit(1)
    )).scalar_one_or_none()
    if latest is not None and (
        latest.status in clusterlib._IN_FLIGHT
        or (not body.force and latest.status == "running" and body.commit_sha
            and latest.commit_sha == body.commit_sha)
    ):
        return {"ok": True, "queued": False, "reason": "already up to date or in flight"}

    from .. import deploy as engine, urls
    dep = Deployment(
        environment_id=env.id, status="queued",
        stack_name=urls.stack_name(project, env), trigger="cluster",
    )
    session.add(dep)
    await session.commit()
    await session.refresh(dep)
    asyncio.get_event_loop().create_task(engine.run_deploy(dep.id, trigger="cluster"))
    log.info("peer deploy accepted from %s: %s/%s", peer["caller_id"],
             body.project_name, body.env_name)
    return {"ok": True, "queued": True, "deployment_id": dep.id}
