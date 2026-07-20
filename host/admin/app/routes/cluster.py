"""Cluster UI API (session-authed) — create/join/status/leave + join-token mint.

The peer-to-peer machinery lives in routes/peer.py + clusterlib; these are the
endpoints the Cluster page calls into.
"""

import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import backuplib, clusterlib, vaultlib
from ..auth import require_session_api
from ..config import settings
from ..db import get_session
from ..models import Identity
from .oauth import build_account_link_start, installation_url

log = logging.getLogger("homebox.cluster.api")

router = APIRouter(prefix="/api/cluster")

# When this node toggles a PEER's serving state, the peer applies it instantly
# but the roster (control plane) only reflects it a heartbeat later — so the UI
# would show stale "enabled" until then. Remember the intent locally and overlay
# it into /api/cluster until the roster agrees (or a safety TTL elapses). Node-
# local + in-memory on purpose: it's this UI's belief, not cluster state, so it
# must not replicate. Self is handled separately via the authoritative flag.
_peer_serving_overrides: dict[str, tuple[bool, float]] = {}  # node_id -> (serving, set_at)
_OVERRIDE_TTL = 300.0  # seconds — cap in case the roster never catches up


def _cp_http(e: clusterlib.ControlPlaneError) -> HTTPException:
    """Map a control-plane error to a clean HTTP response, PRESERVING plan-gating
    (402) and other meaningful statuses with the CP's human-readable detail —
    instead of collapsing everything to a generic 502/500. Unreachable/no-status
    errors stay 502 (bad gateway)."""
    if e.status_code == 402:
        return HTTPException(402, e.detail)
    if e.status_code == 503:
        return HTTPException(503, e.detail)
    return HTTPException(502, str(e))


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
            "node_role": settings.node_role,
            "account_linked": await clusterlib.load_account(session) is not None,
            "control_plane_url": settings.homebox_control_plane_url,
        }
    # Overlay serving state so the UI reflects a toggle immediately, without
    # waiting for the heartbeat→control-plane→roster round-trip:
    #  - this node: its authoritative local flag
    #  - a peer we just toggled: the remembered intent, until the roster agrees
    serving = await clusterlib.get_app_serving(session)
    now = time.time()
    roster = []
    for n in (state.get("roster") or []):
        nid = n.get("node_id")
        # Always surface a role on every entry (default 'peer' for pre-role CPs).
        n = {**n, "role": clusterlib.roster_role(n)}
        if nid == node_id:
            roster.append({**n, "serving": serving})
            continue
        ov = _peer_serving_overrides.get(nid)
        if ov is not None:
            ov_serving, ov_ts = ov
            if now - ov_ts > _OVERRIDE_TTL or bool(n.get("serving")) == ov_serving:
                _peer_serving_overrides.pop(nid, None)  # roster caught up (or TTL) → drop
                roster.append(n)
            else:
                roster.append({**n, "serving": ov_serving})
        else:
            roster.append(n)

    # License: the stored dict extended with derived status fields (plan,
    # features, valid, verified, expires_at, in_grace, expired).
    from .. import licenselib
    status = licenselib.license_status(state)
    lic = dict(state.get("license") or {})
    lic.update(status)

    account_linked = await clusterlib.load_account(session) is not None
    mirror_cache = state.get(clusterlib.MIRROR_CACHE_KEY)
    return {
        "active": True,
        "node_id": node_id,
        "cluster_id": state["cluster_id"],
        "name": state.get("name"),
        "node_name": state.get("node_name"),
        "node_role": settings.node_role,
        "peer_url": state.get("peer_url"),
        "control_plane_url": state.get("control_plane_url"),
        "roster": roster,
        "license": lic,
        "mirror": mirror_cache if isinstance(mirror_cache, dict) else {"status": "none"},
        "account_linked": account_linked,
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
    # D7: clustering requires a linked homebox.sh account. (POST /join stays
    # open — mirror VMs bootstrap through the token join.)
    if not await clusterlib.load_account(session):
        raise HTTPException(
            412, "Link your homebox.sh account before creating a cluster "
                 "(System page → Link Account).")
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
        raise _cp_http(e)
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
        raise _cp_http(e)
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


class SplitBody(BaseModel):
    name: str
    peer_url: str | None = None


@router.post("/split")
async def split_cluster(
    body: SplitBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Split off: leave the current cluster and immediately found a new one
    with this node as the seed, keeping its data and encryption keys. Stacks
    are never torn down, the shared-tunnel connector only stops when other
    nodes remain to serve it, and the account link is untouched."""
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(400, "Cluster name is required.")
    if not await clusterlib.load_cluster(session):
        raise HTTPException(400, "This node is not part of a cluster.")
    peer_url = (body.peer_url or "").strip().rstrip("/")
    if peer_url and not peer_url.startswith("http"):
        peer_url = f"http://{peer_url}"
    try:
        result = await clusterlib.split_off_flow(session, name=name, peer_url=peer_url or None)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except clusterlib.ControlPlaneError as e:
        raise _cp_http(e)  # preserves 402 plan gating for the upgrade notice
    except clusterlib.PeerError as e:
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


class ServingBody(BaseModel):
    node_id: str
    serving: bool


@router.post("/node/serving")
async def set_node_serving(
    body: ServingBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Drain (serving=False) or resume (True) app traffic on a node. Applies
    locally when it targets this node, otherwise dispatches to the peer over the
    LAN peer API — which stays reachable because draining only stops the app
    connector, not the admin/control-plane path."""
    node_id = await clusterlib.get_node_id(session)
    state = await clusterlib.load_cluster(session)
    if not state:
        raise HTTPException(404, "This node is not part of a cluster.")
    # Never let the cluster lose ALL app ingress: refuse to drain the last node
    # that's still serving. At least one connector must stay up for the shared
    # tunnel to have somewhere to route — UNLESS an online mirror is standing by,
    # in which case draining the last serving peer is safe (the mirror
    # auto-promotes within ~3 cycles).
    if not body.serving and not await clusterlib.serving_peers_excluding(session, state, body.node_id):
        if not await clusterlib.online_mirror_standby(session, state, body.node_id):
            raise HTTPException(
                409,
                "Can't disable the last serving node — at least one node must keep "
                "serving app traffic. Enable another node (or add an online mirror) first.",
            )
    if body.node_id == node_id:
        result = await clusterlib.apply_app_serving(session, body.serving)
        return {"ok": True, "node_id": node_id, **result}
    peer = next((n for n in state.get("roster") or [] if n.get("node_id") == body.node_id), None)
    if not peer or not peer.get("peer_url"):
        raise HTTPException(404, "Unknown node or it has no peer URL.")
    try:
        resp = await clusterlib.peer_request(
            "POST", peer["peer_url"], "/peer/set-serving",
            secret=clusterlib.cluster_secret(state), self_node_id=node_id,
            body={"serving": body.serving},
        )
    except clusterlib.PeerError as e:
        raise HTTPException(502, str(e))
    # Remember the intent so this UI shows the peer's new state right away.
    _peer_serving_overrides[body.node_id] = (body.serving, time.time())
    return {"ok": True, "node_id": body.node_id, **resp}


# ───── homebox.sh account (token-less create/join) ────────────────────────────


async def _suggested_link_identity(session: AsyncSession) -> dict[str, str] | None:
    """The most recently used OAuth identity, for one-click account linking on
    an unlinked node: {"provider", "email"}, or None when nobody has signed in
    via OAuth yet."""
    row = (await session.execute(
        select(Identity)
        .where(Identity.enabled == True,  # noqa: E712
               Identity.last_login_provider.is_not(None),
               Identity.last_login_at.is_not(None))
        .order_by(Identity.last_login_at.desc())
        .limit(1)
    )).scalar_one_or_none()
    if row is None:
        return None
    return {"provider": row.last_login_provider, "email": row.email}


@router.get("/account")
async def account_status(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    acct = await clusterlib.load_account(session)
    if not acct:
        return {"linked": False, "suggested": await _suggested_link_identity(session)}
    overview = await clusterlib._get_setting(session, clusterlib.ACCOUNT_OVERVIEW_KEY)
    overview = overview if isinstance(overview, dict) else {}
    account = overview.get("account")
    account = account if isinstance(account, dict) else {}
    backup = await clusterlib._get_setting(session, backuplib.CLOUD_BACKUP_KEY)
    backup = backup if isinstance(backup, dict) else {}
    vault = await vaultlib.get_vault_state(session)
    post_link = await vaultlib.get_post_link_state(session)
    return {
        "linked": True,
        "control_plane_url": acct.get("control_plane_url"),
        "node_name": acct.get("node_name"),
        "peer_url": acct.get("peer_url"),
        "linked_at": acct.get("linked_at"),
        "overview": overview,
        "email": account.get("email"),
        "plan": account.get("plan"),
        "backup": {"pushed_at": backup.get("pushed_at"), "error": backup.get("error")},
        "vault": {"version": vault.get("version"), "pushed_at": vault.get("pushed_at"),
                  "pulled_at": vault.get("pulled_at"), "error": vault.get("error")},
        # Post-link pipeline progress (link → sync → identity → cluster).
        "post_link": post_link or None,
        "suggested": None,  # already linked — nothing to suggest
    }


@router.get("/account/oauth-url")
async def account_oauth_url(
    provider: str,
    request: Request,
    response: Response,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Mint the oauth-proxy /start URL for signing in / signing up a homebox.sh
    account from the admin (the frontend opens {"url"} in a popup). Reuses the
    node-login OAuth machinery: same signed-state + CSRF cookie, same callback
    origin — the state carries mode=account-link so /oauth/callback routes it to
    the account-link branch instead of a node login."""
    provider = provider.lower()
    if provider not in ("github", "google"):
        raise HTTPException(400, "Unknown provider")
    installation = await installation_url(request, session)
    url = build_account_link_start(response, provider, user, installation)
    return {"url": url}


@router.get("/account/providers")
async def account_providers(user: str = Depends(require_session_api)):
    """Which OAuth providers the proxy can drive, for rendering the sign-in
    buttons. Falls back to GitHub-only when the proxy is unreachable."""
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{settings.homebox_oauth_proxy_url}/providers")
            if r.status_code == 200:
                data = r.json()
                return {"github": bool(data.get("github")), "google": bool(data.get("google"))}
    except (httpx.HTTPError, OSError, ValueError):
        pass
    return {"github": True, "google": False}


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
    # The post-link pipeline (vault restore → identity auto-create → default
    # new cluster) runs in the background (own session) so this response
    # returns immediately; progress rides the post_link/vault_state settings.
    vaultlib.schedule_post_link()
    return {"ok": True}


class LinkSilentBody(BaseModel):
    provider: str | None = None  # restrict to one provider's stored tokens


@router.post("/account/link-silent")
async def account_link_silent(
    body: LinkSilentBody | None = None,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """G4: link this node to a homebox.sh account WITHOUT a browser round-trip,
    by re-authing with a stored (encrypted) provider token from an earlier
    OAuth login/link. Tries the freshest usable token first; a token the CP
    rejects as invalid/expired (401) is deleted. 412 when nothing usable
    remains — the UI falls back to the inline AccountAuthModal."""
    import socket

    body = body or LinkSilentBody()
    state = await clusterlib.load_cluster(session)
    acct = await clusterlib.load_account(session)
    cp_url = ((state or {}).get("control_plane_url")
              or settings.homebox_control_plane_url)
    node_name = (
        (acct or {}).get("node_name") or (state or {}).get("node_name")
        or socket.gethostname() or "homebox"
    )
    peer_url = (acct or {}).get("peer_url") or (state or {}).get("peer_url") or ""

    from .. import crypto
    candidates = clusterlib.freshest_provider_tokens(
        await clusterlib.load_provider_tokens(session), body.provider)
    for key, entry in candidates:
        provider, _, email = key.partition(":")
        access_token = crypto.decrypt(entry.get("token_encrypted") or "")
        if not access_token:
            # Undecryptable (e.g. key rotation) — useless forever, drop it.
            await clusterlib.delete_provider_token(session, key)
            continue
        try:
            reg = await clusterlib._cp(
                "POST", cp_url, "/v1/accounts/register",
                body={"provider": provider, "access_token": access_token,
                      "label": f"node {node_name}"},
            )
        except clusterlib.ControlPlaneError as e:
            if e.status_code == 401:
                # Provider token invalid/expired — forget it, try the next.
                await clusterlib.delete_provider_token(session, key)
                continue
            # Pass other CP errors through (402 plan gate, 502 provider down…).
            if e.status_code and 400 <= e.status_code < 500:
                raise HTTPException(e.status_code, e.detail)
            raise HTTPException(502, str(e))
        account_token = (reg.get("account_token") or "").strip()
        if not account_token:
            raise HTTPException(502, "The account service returned no token.")
        try:
            await clusterlib.link_account_flow(
                session,
                control_plane_url=cp_url,
                account_token_plain=account_token,
                node_name=node_name,
                peer_url=peer_url,
            )
        except clusterlib.ControlPlaneError as e:
            raise _cp_http(e)
        linked_email = (reg.get("email") or email or "").strip().lower() or None
        # Same post-link path as the OAuth account-link flow.
        vaultlib.schedule_post_link(provider=provider, email=linked_email)
        return {"ok": True, "provider": provider, "email": linked_email}
    raise HTTPException(412, "no stored provider token")


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
        # D7: clustering requires a linked account.
        raise HTTPException(
            412, "Link your homebox.sh account before creating a cluster "
                 "(System page → Link Account).")
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
        raise _cp_http(e)
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
    except clusterlib.ControlPlaneError as e:
        raise _cp_http(e)
    except clusterlib.PeerError as e:
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
        raise _cp_http(e)
    return {"ok": True, "invited": body.node_id, "cluster_id": cluster_id}


# ───── fleet god view: topology + remote-op directives ────────────────────────


@router.get("/account/topology")
async def account_topology(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """The account-wide fleet view, proxied from the control plane and
    annotated with this node's identity, pending cloud-node provisions, and
    local vault sync freshness."""
    acct = await clusterlib.load_account(session)
    if not acct:
        raise HTTPException(412, "Link your homebox.sh account to see the fleet topology.")
    from .. import crypto
    try:
        topo = await clusterlib._cp(
            "GET", acct["control_plane_url"], "/v1/accounts/topology",
            token=crypto.decrypt(acct["token_encrypted"]))
    except clusterlib.ControlPlaneError as e:
        raise _cp_http(e)
    topo["this_node_id"] = await clusterlib.get_node_id(session)
    provisions = await clusterlib._get_setting(session, "node_provisions")
    topo["provisions"] = provisions if isinstance(provisions, list) else []
    vault = await vaultlib.get_vault_state(session)
    topo["vault_state"] = {"pushed_at": vault.get("pushed_at"),
                           "pulled_at": vault.get("pulled_at"),
                           "error": vault.get("error")}
    return topo


class DirectiveBody(BaseModel):
    node_id: str
    type: str
    payload: dict = {}


@router.post("/account/directives")
async def account_directive(
    body: DirectiveBody,
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Queue a remote op (set_serving / split_off / split_cluster) for another
    linked node — it executes on that node's next account poll (≤60s)."""
    acct = await clusterlib.load_account(session)
    if not acct:
        raise HTTPException(412, "Link your homebox.sh account first.")
    from .. import crypto
    try:
        resp = await clusterlib._cp(
            "POST", acct["control_plane_url"], "/v1/accounts/directives",
            token=crypto.decrypt(acct["token_encrypted"]),
            body={"node_id": body.node_id, "type": body.type,
                  "payload": body.payload or {}})
    except clusterlib.ControlPlaneError as e:
        # Pass CP 4xx (402 plan gate, 400 validation, …) through with detail.
        if e.status_code and 400 <= e.status_code < 500:
            raise HTTPException(e.status_code, e.detail)
        raise HTTPException(502, str(e))
    return resp


# ───── premium: billing upgrade + cloud mirror ────────────────────────────────


async def _account_token(session: AsyncSession) -> tuple[str, str]:
    """(account token plain, control_plane_url) for the linked homebox.sh
    account, or raise 409 if unlinked."""
    from .. import crypto
    acct = await clusterlib.load_account(session)
    if not acct:
        raise HTTPException(409, "Link your homebox.sh account first")
    return crypto.decrypt(acct["token_encrypted"]), acct["control_plane_url"]


@router.post("/upgrade")
async def upgrade(
    user: str = Depends(require_session_api),
):
    """Plan management moved to the website — the node no longer launches Stripe
    checkout. Always hand back the cloud page URL; the caller redirects there.
    No account/cluster preconditions."""
    return {"url": f"{settings.homebox_site_url}/cloud"}


async def _mirror_ctx(session: AsyncSession) -> tuple[str, str, str]:
    """(account_token, control_plane_url, cluster_id) for mirror calls; 409 when
    no account is linked or this node isn't in a cluster."""
    token, cp_url = await _account_token(session)
    state = await clusterlib.load_cluster(session)
    if not state:
        raise HTTPException(409, "This node is not part of a cluster.")
    return token, state["control_plane_url"], state["cluster_id"]


@router.get("/mirror")
async def mirror_status(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Cloud-mirror status for this cluster (proxied from the control plane)."""
    token, cp_url, cluster_id = await _mirror_ctx(session)
    try:
        resp = await clusterlib._cp("GET", cp_url, f"/v1/clusters/{cluster_id}/mirror", token=token)
    except clusterlib.ControlPlaneError as e:
        raise _cp_http(e)
    # Refresh the cheap-read cache on the cluster blob too.
    state = await clusterlib.load_cluster(session)
    if state is not None:
        state[clusterlib.MIRROR_CACHE_KEY] = resp
        await clusterlib.save_cluster(session, state)
        await session.commit()
    return resp


@router.post("/mirror")
async def mirror_enable(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Enable the cloud mirror for this cluster (Premium)."""
    token, cp_url, cluster_id = await _mirror_ctx(session)
    try:
        resp = await clusterlib._cp("POST", cp_url, f"/v1/clusters/{cluster_id}/mirror", token=token)
    except clusterlib.ControlPlaneError as e:
        raise _cp_http(e)
    return resp


@router.delete("/mirror")
async def mirror_disable(
    user: str = Depends(require_session_api),
    session: AsyncSession = Depends(get_session),
):
    """Disable the cloud mirror for this cluster."""
    token, cp_url, cluster_id = await _mirror_ctx(session)
    try:
        resp = await clusterlib._cp("DELETE", cp_url, f"/v1/clusters/{cluster_id}/mirror", token=token)
    except clusterlib.ControlPlaneError as e:
        raise _cp_http(e)
    return resp
