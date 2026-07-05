"""Cluster membership + coordination for this node.

A homebox cluster is a set of nodes that all serve the same apps
(active-active). Membership is brokered by the control plane
(cluster.homebox.sh); everything with actual data in it flows node-to-node:

  - the cluster ENCRYPTION_KEY / APP_SECRET travel sealed to the joining
    node's X25519 key (crypto.seal_to) — the control plane never sees them
  - config (integrations, projects, domains, …) syncs via the peer API
    (routes/peer.py + cluster_sync.py) over the LAN, through each node's
    Traefik on :80 using the Host header `homebox-peer.internal`
  - deploys fan out peer-to-peer (deploy.py calls fanout_deploy)

State lives in the settings table:
  node_keys  (node-scoped)  this node's X25519 keypair
  cluster    (node-scoped)  membership: ids, tokens (encrypted), roster cache

The background cluster_loop heartbeats to the control plane (which doubles as
the roster rendezvous), runs the initial config sync after a join, and
periodically reconciles deployments so a node that was offline catches up.
"""

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import secrets as _secrets
import time
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import crypto
from .config import settings, CLUSTER_KEYS_FILE
from .db import SessionLocal
from .host import PEER_HOST, restart_container, write_traefik_dynamic
from .models import Deployment, Environment, Integration, Project, Setting

log = logging.getLogger("homebox.cluster")

NODE_KEYS_KEY = "node_keys"
CLUSTER_KEY = "cluster"
ACCOUNT_KEY = "account"
ACCOUNT_OVERVIEW_KEY = "account_overview"
INSTALL_ID_KEY = "install_id"
ADMIN_DOMAIN_KEY = "admin_domain"

VERSION = "0.2-cluster"
LOOP_INTERVAL = 60          # heartbeat cadence (seconds)
RECONCILE_EVERY = 5         # deployment reconcile every N cycles
PEER_TOKEN_WINDOW = 600     # peer auth token validity (seconds)


# ───── settings-table helpers ─────────────────────────────────────────────────


async def _get_setting(session: AsyncSession, key: str):
    row = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    return row.value if row else None


async def _set_setting(session: AsyncSession, key: str, value) -> None:
    row = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    await session.commit()


async def get_node_id(session: AsyncSession) -> str:
    """This install's stable random identifier (shared with the tunnel module,
    which tags Cloudflare tunnels with it). Doubles as the cluster node id."""
    val = await _get_setting(session, INSTALL_ID_KEY)
    if isinstance(val, dict) and val.get("value"):
        return str(val["value"])
    new_id = _secrets.token_urlsafe(16)
    await _set_setting(session, INSTALL_ID_KEY, {"value": new_id})
    return new_id


async def get_node_keys(session: AsyncSession) -> tuple[str, str]:
    """(private_hex, public_hex), generated on first use."""
    val = await _get_setting(session, NODE_KEYS_KEY)
    if isinstance(val, dict) and val.get("private") and val.get("public"):
        return val["private"], val["public"]
    priv, pub = crypto.generate_keypair()
    await _set_setting(session, NODE_KEYS_KEY, {"private": priv, "public": pub})
    return priv, pub


async def load_cluster(session: AsyncSession) -> dict[str, Any] | None:
    """The raw cluster membership blob, or None when not in a cluster."""
    val = await _get_setting(session, CLUSTER_KEY)
    return val if isinstance(val, dict) and val.get("cluster_id") else None


async def save_cluster(session: AsyncSession, state: dict[str, Any]) -> None:
    await _set_setting(session, CLUSTER_KEY, state)


async def clear_cluster(session: AsyncSession) -> None:
    await _set_setting(session, CLUSTER_KEY, {})


def cluster_secret(state: dict[str, Any]) -> str:
    return crypto.decrypt(state.get("cluster_secret_encrypted") or "")


def node_token(state: dict[str, Any]) -> str:
    return crypto.decrypt(state.get("node_token_encrypted") or "")


def account_token(state: dict[str, Any]) -> str:
    return crypto.decrypt(state.get("account_token_encrypted") or "")


def peers(state: dict[str, Any], self_node_id: str) -> list[dict[str, Any]]:
    """Roster minus self."""
    return [n for n in (state.get("roster") or []) if n.get("node_id") != self_node_id]


# ───── control-plane client ───────────────────────────────────────────────────


class ControlPlaneError(Exception):
    pass


async def _cp(method: str, base: str, path: str, *, token: str | None = None,
              body: dict | None = None) -> dict:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = base.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.request(method, url, headers=headers, json=body)
    except httpx.HTTPError as e:
        raise ControlPlaneError(f"control plane unreachable at {base}: {e}")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:200]
        raise ControlPlaneError(f"control plane: {detail} (HTTP {r.status_code})")
    return r.json()


# ───── peer auth + client ─────────────────────────────────────────────────────
#
# Peer requests are authenticated with an HMAC bearer derived from the shared
# cluster secret: hb1.<node_id>.<unix_ts>.<hmac_sha256(secret, node_id.ts)>.
# Valid within ±PEER_TOKEN_WINDOW. (LAN-grade; the WireGuard mesh + mTLS from
# the design doc harden this for cross-network clusters later.)


def make_peer_token(secret: str, self_node_id: str) -> str:
    ts = str(int(time.time()))
    sig = hmac.new(secret.encode(), f"{self_node_id}.{ts}".encode(), hashlib.sha256).hexdigest()
    return f"hb1.{self_node_id}.{ts}.{sig}"


def verify_peer_token(secret: str, token: str) -> str | None:
    """Returns the calling node_id, or None."""
    parts = (token or "").split(".")
    if len(parts) != 4 or parts[0] != "hb1" or not secret:
        return None
    _, nid, ts, sig = parts
    try:
        if abs(time.time() - int(ts)) > PEER_TOKEN_WINDOW:
            return None
    except ValueError:
        return None
    expected = hmac.new(secret.encode(), f"{nid}.{ts}".encode(), hashlib.sha256).hexdigest()
    return nid if hmac.compare_digest(expected, sig) else None


class PeerError(Exception):
    pass


async def peer_request(method: str, peer_url: str, path: str, *, secret: str,
                       self_node_id: str, body: dict | None = None,
                       timeout: float = 30) -> dict:
    """Call another node's peer API through its Traefik (Host-header routed)."""
    headers = {
        "Host": PEER_HOST,
        "Authorization": f"Bearer {make_peer_token(secret, self_node_id)}",
    }
    url = peer_url.rstrip("/") + path
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.request(method, url, headers=headers, json=body)
    except httpx.HTTPError as e:
        raise PeerError(f"peer {peer_url} unreachable: {e}")
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text[:200]
        raise PeerError(f"peer {peer_url}: {detail} (HTTP {r.status_code})")
    return r.json()


# ───── traefik peer route ─────────────────────────────────────────────────────


async def ensure_peer_route(session: AsyncSession) -> None:
    """Regenerate the Traefik file-provider config (admin route if onboarded,
    plus the always-on peer route write_traefik_dynamic appends)."""
    admin_domain = await _get_setting(session, ADMIN_DOMAIN_KEY)
    routes = []
    if isinstance(admin_domain, str) and admin_domain.strip():
        routes.append({
            "name": "homebox-admin",
            "host": admin_domain.strip(),
            "service_url": "http://homebox-admin:8000",
        })
    write_traefik_dynamic(routes)


# ───── create / join ──────────────────────────────────────────────────────────


async def create_cluster_flow(
    session: AsyncSession, *, control_plane_url: str, account_token_plain: str,
    name: str, peer_url: str, node_name: str,
) -> dict[str, Any]:
    """Found a cluster with this node as the seed. The cluster adopts this
    node's existing ENCRYPTION_KEY/APP_SECRET; we mint the shared peer secret
    here (the control plane never sees it)."""
    node_id = await get_node_id(session)
    _, pub = await get_node_keys(session)
    resp = await _cp("POST", control_plane_url, "/v1/clusters",
                     token=account_token_plain,
                     body={"name": name, "node_id": node_id, "node_name": node_name,
                           "pubkey": pub, "peer_url": peer_url, "version": VERSION})
    state = {
        "cluster_id": resp["cluster_id"],
        "name": resp["name"],
        "control_plane_url": control_plane_url.rstrip("/"),
        "peer_url": peer_url,
        "node_name": node_name,
        "node_token_encrypted": crypto.encrypt(resp["node_token"]),
        "cluster_secret_encrypted": crypto.encrypt(_secrets.token_hex(32)),
        "account_token_encrypted": crypto.encrypt(account_token_plain),
        "roster": resp["nodes"],
        "license": resp.get("license"),
        "initial_sync_done": True,  # seed node IS the source of truth
        "joined_at": datetime.utcnow().isoformat(),
    }
    await save_cluster(session, state)
    # Make the current keys explicit on disk so every future boot (and any
    # compose recreate with stale env) stays on the cluster keys.
    _write_cluster_keys(settings.encryption_key, settings.app_secret)
    await ensure_peer_route(session)
    return state


def _write_cluster_keys(encryption_key: str, app_secret: str) -> None:
    CLUSTER_KEYS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CLUSTER_KEYS_FILE.write_text(json.dumps(
        {"encryption_key": encryption_key, "app_secret": app_secret}, indent=2
    ) + "\n")


async def _reencrypt_local_secrets(session: AsyncSession, old_key: str, new_key: str) -> None:
    """Re-key every encrypted blob so it opens under the cluster key after the
    restart. Best-effort per field — an undecryptable blob is left as-is."""
    def rekey(token: str | None) -> str | None:
        if not token:
            return token
        plain = crypto.decrypt_with(old_key, token)
        return crypto.encrypt_with(new_key, plain) if plain else token

    for integ in (await session.execute(select(Integration))).scalars():
        integ.secret_encrypted = rekey(integ.secret_encrypted)
        cfg = dict(integ.config or {})
        changed = False
        for field in ("token_encrypted", "connector_token_encrypted"):
            if cfg.get(field):
                cfg[field] = rekey(cfg[field])
                changed = True
        if changed:
            integ.config = cfg
    wh = await _get_setting(session, "webhook")
    if isinstance(wh, dict) and wh.get("secret_encrypted"):
        wh = dict(wh)
        wh["secret_encrypted"] = rekey(wh["secret_encrypted"])
        await _set_setting(session, "webhook", wh)
    await session.commit()


async def join_cluster_flow(
    session: AsyncSession, *, control_plane_url: str, join_token: str,
    peer_url: str, node_name: str,
) -> dict[str, Any]:
    """Join an existing cluster: register with the control plane, handshake
    with a member node for the sealed cluster keys, adopt them, then schedule
    a self-restart (the initial config sync runs after the restart)."""
    join_token = join_token.strip()
    parts = join_token.split(".")
    if len(parts) != 3 or parts[0] != "hbj":
        raise ControlPlaneError("That doesn't look like a homebox join token (hbj.<cluster>.<secret>).")
    cluster_id = parts[1]

    node_id = await get_node_id(session)
    priv, pub = await get_node_keys(session)

    reg = await _cp("POST", control_plane_url, f"/v1/clusters/{cluster_id}/nodes",
                    body={"join_token": join_token, "node_id": node_id,
                          "node_name": node_name, "pubkey": pub,
                          "peer_url": peer_url, "version": VERSION})

    donors = [n for n in reg["nodes"] if n["node_id"] != node_id and n.get("peer_url")]
    if not donors:
        raise PeerError("No reachable member node found in the cluster roster.")

    sealed = None
    errors: list[str] = []
    for donor in donors:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.post(
                    donor["peer_url"].rstrip("/") + "/peer/handshake",
                    headers={"Host": PEER_HOST},
                    json={"cluster_id": cluster_id, "node_id": node_id,
                          "pubkey": pub, "grant": reg["grant"]},
                )
            if r.status_code >= 400:
                errors.append(f"{donor['peer_url']}: HTTP {r.status_code} {r.text[:200]}")
                continue
            sealed = r.json().get("sealed")
            break
        except httpx.HTTPError as e:
            errors.append(f"{donor['peer_url']}: {e}")
    if not sealed:
        raise PeerError(
            "Couldn't reach any cluster member for the key handshake. "
            "Check LAN connectivity to: " + "; ".join(errors)
        )

    payload = crypto.unseal(priv, sealed)
    new_enc, new_app = payload["encryption_key"], payload["app_secret"]

    # Re-key local secrets, then persist membership ENCRYPTED WITH THE CLUSTER
    # KEY — after the restart the app decrypts with it.
    await _reencrypt_local_secrets(session, settings.encryption_key, new_enc)
    state = {
        "cluster_id": cluster_id,
        "name": reg.get("name") or "",
        "control_plane_url": control_plane_url.rstrip("/"),
        "peer_url": peer_url,
        "node_name": node_name,
        "node_token_encrypted": crypto.encrypt_with(new_enc, reg["node_token"]),
        "cluster_secret_encrypted": crypto.encrypt_with(new_enc, payload["cluster_secret"]),
        "account_token_encrypted": crypto.encrypt_with(new_enc, payload.get("account_token") or ""),
        "roster": reg["nodes"],
        "license": reg.get("license"),
        "initial_sync_done": False,
        "joined_at": datetime.utcnow().isoformat(),
    }
    await save_cluster(session, state)
    _write_cluster_keys(new_enc, new_app)
    await ensure_peer_route(session)
    restart_self_soon()
    return state


def restart_self_soon(delay: float = 2.0) -> None:
    """Restart our own container shortly — lets the HTTP response flush first.
    config.py re-reads cluster-keys.json on boot."""
    async def _later():
        await asyncio.sleep(delay)
        log.warning("cluster join: restarting admin to adopt cluster keys")
        await asyncio.to_thread(restart_container, "homebox-admin")
    asyncio.get_event_loop().create_task(_later())


# ───── homebox.sh account link (token-less create/join UX) ───────────────────
#
# A node linked to a homebox.sh account can, from its Cluster page, see every
# other linked node and every cluster on the account, create clusters, join
# one directly, or invite another node — the control plane delivers the
# invite as a directive the target node picks up on its next poll and joins
# automatically. Join tokens still work as the manual fallback.


async def load_account(session: AsyncSession) -> dict[str, Any] | None:
    val = await _get_setting(session, ACCOUNT_KEY)
    return val if isinstance(val, dict) and val.get("token_encrypted") else None


async def link_account_flow(
    session: AsyncSession, *, control_plane_url: str, account_token_plain: str,
    node_name: str, peer_url: str,
) -> dict[str, Any]:
    node_id = await get_node_id(session)
    _, pub = await get_node_keys(session)
    await _cp("POST", control_plane_url, "/v1/accounts/nodes",
              token=account_token_plain,
              body={"node_id": node_id, "name": node_name, "pubkey": pub,
                    "peer_url": peer_url, "version": VERSION})
    blob = {
        "control_plane_url": control_plane_url.rstrip("/"),
        "token_encrypted": crypto.encrypt(account_token_plain),
        "node_name": node_name,
        "peer_url": peer_url,
        "linked_at": datetime.utcnow().isoformat(),
    }
    await _set_setting(session, ACCOUNT_KEY, blob)
    await account_poll(session, blob)
    return blob


async def unlink_account(session: AsyncSession) -> None:
    acct = await load_account(session)
    if acct:
        try:
            node_id = await get_node_id(session)
            await _cp("DELETE", acct["control_plane_url"], f"/v1/accounts/nodes/{node_id}",
                      token=crypto.decrypt(acct["token_encrypted"]))
        except ControlPlaneError as e:
            log.warning("account unlink at control plane failed: %s", e)
    await _set_setting(session, ACCOUNT_KEY, {})
    await _set_setting(session, ACCOUNT_OVERVIEW_KEY, {})


async def account_poll(session: AsyncSession, acct: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Refresh the account overview cache; returns it (or None when unlinked).
    Also surfaces any pending join directive for this node."""
    acct = acct or await load_account(session)
    if not acct:
        return None
    node_id = await get_node_id(session)
    overview = await _cp(
        "POST", acct["control_plane_url"], "/v1/accounts/poll",
        token=crypto.decrypt(acct["token_encrypted"]),
        body={"node_id": node_id, "peer_url": acct.get("peer_url") or "",
              "version": VERSION, "name": acct.get("node_name") or ""},
    )
    overview["polled_at"] = datetime.utcnow().isoformat()
    await _set_setting(session, ACCOUNT_OVERVIEW_KEY, overview)
    return overview


async def _maybe_autojoin(session: AsyncSession, overview: dict[str, Any]) -> None:
    """Execute a pending join directive (invite from another node)."""
    directive = overview.get("directive")
    if not directive:
        return
    if await load_cluster(session):
        return  # already in a cluster — the directive stays pending until leave
    acct = await load_account(session)
    if not acct:
        return
    log.info("account directive: joining cluster %s (%s)",
             directive.get("cluster_id"), directive.get("cluster_name"))
    await join_cluster_flow(
        session,
        control_plane_url=acct["control_plane_url"],
        join_token=directive["join_token"],
        peer_url=acct.get("peer_url") or "",
        node_name=acct.get("node_name") or "",
    )


# ───── leave / disconnect ─────────────────────────────────────────────────────


async def leave_cluster_flow(
    session: AsyncSession, *, stop_tunnel: bool = True, teardown_stacks: bool = False,
) -> dict[str, Any]:
    """Fully disconnect this node: tell peers (they drop their subs to us,
    releasing our WAL slots on their side), drop our own subscriptions, stop
    serving the shared tunnel, optionally tear down cluster-enabled stacks,
    then deregister. Every step is best-effort — leave always completes."""
    from . import cluster_db
    from .deploy import repo_dir, teardown_stack
    from .urls import stack_name as make_stack_name

    state = await load_cluster(session)
    if not state:
        raise PeerError("This node is not part of a cluster.")
    node_id = await get_node_id(session)
    secret = cluster_secret(state)
    result: dict[str, Any] = {"peers_notified": [], "subs_dropped": [],
                              "stacks_torn_down": [], "tunnel_stopped": False}

    # 1. peers drop their subscriptions to us (must happen while we're still
    #    in their roster so our peer token verifies)
    for peer in peers(state, node_id):
        if not peer.get("peer_url"):
            continue
        try:
            await peer_request("POST", peer["peer_url"], "/peer/node-leaving",
                               secret=secret, self_node_id=node_id,
                               body={"node_id": node_id}, timeout=60)
            result["peers_notified"].append(peer.get("name") or peer["node_id"])
        except PeerError as e:
            log.warning("leave: peer notify failed (their reconcile will catch up): %s", e)

    # 2. drop OUR subscriptions + optionally tear the stacks down
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
            try:
                result["subs_dropped"] += await cluster_db.drop_subscriptions(stack=stack, info=info)
            except Exception:  # noqa: BLE001
                log.exception("leave: sub drop failed for %s", stack)
        if teardown_stacks:
            ok, _out = await teardown_stack(project.name, env.name)
            if ok:
                dep.status = "stopped"
                result["stacks_torn_down"].append(stack)
    if teardown_stacks:
        await session.commit()

    # 3. stop serving the shared tunnel: remove the connector AND forget the
    #    connector token locally so the monitor doesn't resurrect it. (The
    #    Cloudflare integration row itself stays — it may hold the account
    #    token for other purposes; a single-node install keeps working.)
    if stop_tunnel:
        from . import cloudflare as cf
        from .host import remove_container
        cf_state = await cf.load_state(session)
        if cf_state.get("connector_token_encrypted"):
            cf_state.pop("connector_token_encrypted", None)
            await cf.save_state(session, cf_state)
        remove_container("homebox-cloudflared")
        result["tunnel_stopped"] = True

    # 4. deregister + forget membership
    acct_token = account_token(state)
    if acct_token:
        try:
            await _cp("DELETE", state["control_plane_url"],
                      f"/v1/clusters/{state['cluster_id']}/nodes/{node_id}",
                      token=acct_token)
        except ControlPlaneError as e:
            log.warning("leave: control-plane deregister failed: %s", e)
    await clear_cluster(session)
    log.info("left cluster %s: %s", state["cluster_id"], result)
    return result


async def evict_node(session: AsyncSession, node_id_to_evict: str) -> dict[str, Any]:
    """Remove another (typically dead) node at the control plane. Every
    surviving node's reconcile then drops its subscriptions to the departed
    ordinal, releasing WAL slots."""
    state = await load_cluster(session)
    if not state:
        raise PeerError("This node is not part of a cluster.")
    acct = account_token(state) or ""
    if not acct:
        blob = await load_account(session)
        acct = crypto.decrypt(blob["token_encrypted"]) if blob else ""
    if not acct:
        raise ControlPlaneError("No account credential on this node — evict from the founding node.")
    resp = await _cp("DELETE", state["control_plane_url"],
                     f"/v1/clusters/{state['cluster_id']}/nodes/{node_id_to_evict}",
                     token=acct)
    state["roster"] = resp.get("nodes") or state.get("roster")
    await save_cluster(session, state)
    # Clean up our side right away rather than waiting for the next cycle.
    await ensure_db_replication(session, state)
    return resp


# ───── steady-state loop ──────────────────────────────────────────────────────


async def _heartbeat(session: AsyncSession, state: dict[str, Any]) -> dict[str, Any]:
    node_id = await get_node_id(session)
    resp = await _cp(
        "POST", state["control_plane_url"],
        f"/v1/clusters/{state['cluster_id']}/heartbeat",
        token=node_token(state),
        body={"node_id": node_id, "peer_url": state.get("peer_url") or "",
              "version": VERSION, "name": state.get("node_name") or ""},
    )
    state["roster"] = resp["nodes"]
    state["license"] = resp.get("license")
    state["last_heartbeat"] = datetime.utcnow().isoformat()
    await save_cluster(session, state)
    return state


async def initial_sync(session: AsyncSession, state: dict[str, Any]) -> bool:
    """Pull full config from the first reachable peer and import it. Returns
    True once done (flag persisted)."""
    from . import cluster_sync
    node_id = await get_node_id(session)
    secret = cluster_secret(state)
    for peer in peers(state, node_id):
        if not peer.get("peer_url"):
            continue
        try:
            export = await peer_request(
                "GET", peer["peer_url"], "/peer/state",
                secret=secret, self_node_id=node_id,
            )
        except PeerError as e:
            log.warning("initial sync: %s", e)
            continue
        summary = await cluster_sync.import_state(session, export, mode="full")
        state["initial_sync_done"] = True
        state["last_sync_at"] = datetime.utcnow().isoformat()
        await save_cluster(session, state)
        log.info("initial cluster sync from %s complete: %s", peer["peer_url"], summary)
        # admin_domain just synced in — rewrite the Traefik file so THIS node
        # serves the admin hostname too (tunnel traffic can land on any node).
        await ensure_peer_route(session)
        await reconcile_deployments(session, export)
        return True
    return False


async def _queue_cluster_deploy(session: AsyncSession, env: Environment) -> None:
    from . import deploy as engine, urls
    project = await session.get(Project, env.project_id)
    dep = Deployment(
        environment_id=env.id, status="queued",
        stack_name=urls.stack_name(project, env), trigger="cluster",
    )
    session.add(dep)
    await session.commit()
    await session.refresh(dep)
    asyncio.get_event_loop().create_task(engine.run_deploy(dep.id, trigger="cluster"))


_IN_FLIGHT = ("queued", "cloning", "dissecting", "building", "starting")


async def reconcile_deployments(session: AsyncSession, export: dict[str, Any]) -> int:
    """Bring this node's stacks up to date with a peer's view: any env the
    cluster has running that we don't (same sha) gets deployed locally. This is
    what catches a node up after downtime — and what deploys everything right
    after a join."""
    queued = 0
    for entry in export.get("deployments") or []:
        if entry.get("status") != "running" or not entry.get("commit_sha"):
            continue
        project = (await session.execute(
            select(Project).where(Project.name == entry["project_name"]) )).scalar_one_or_none()
        if not project or not project.managed:
            continue
        env = (await session.execute(
            select(Environment).where(
                Environment.project_id == project.id,
                Environment.name == entry["env_name"],
            ))).scalar_one_or_none()
        if not env:
            continue
        latest = (await session.execute(
            select(Deployment).where(Deployment.environment_id == env.id)
            .order_by(Deployment.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        if latest is not None and (
            latest.status in _IN_FLIGHT
            or (latest.status == "running" and latest.commit_sha == entry["commit_sha"])
        ):
            continue
        log.info("cluster reconcile: deploying %s/%s @ %s",
                 entry["project_name"], entry["env_name"], entry["commit_sha"][:8])
        await _queue_cluster_deploy(session, env)
        queued += 1
    return queued


async def reconcile_from_peers(session: AsyncSession, state: dict[str, Any]) -> None:
    from . import cluster_sync
    node_id = await get_node_id(session)
    secret = cluster_secret(state)
    for peer in peers(state, node_id):
        if not (peer.get("peer_url") and peer.get("online")):
            continue
        try:
            export = await peer_request("GET", peer["peer_url"], "/peer/state",
                                        secret=secret, self_node_id=node_id)
        except PeerError as e:
            log.warning("reconcile: %s", e)
            continue
        await cluster_sync.import_state(session, export, mode="update")
        await reconcile_deployments(session, export)
        state["last_sync_at"] = datetime.utcnow().isoformat()
        await save_cluster(session, state)
        return


async def ensure_db_replication(session: AsyncSession, state: dict[str, Any]) -> None:
    """Re-ensure Spock wiring (repset tables + peer subscriptions) for every
    running cluster-enabled stack. Heals ordering: a peer that deployed after
    us couldn't be subscribed to at our deploy time — this catches it up."""
    from . import cluster_db
    from .deploy import repo_dir
    from .urls import stack_name as make_stack_name
    node_id = await get_node_id(session)
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
        secret = cluster_secret(state)
        for info in cluster_db.infos_from_compose(rd):
            # The compose env carries the password derived at deploy time; the
            # cluster secret may have changed since (cluster re-created).
            # Always reconcile against the CURRENT derivation — 1b in
            # ensure_replication rotates the local role to match.
            info["repl_password"] = cluster_db.derive_repl_password(
                secret, project.name, env.name, info["service"],
            )
            try:
                res = await cluster_db.ensure_replication(
                    stack=stack, info=info, state=state, self_node_id=node_id,
                )
                if res.get("subs_created") or res.get("tables_added"):
                    log.info("cluster db reconcile %s/%s: +tables %s +subs %s",
                             project.name, env.name,
                             res["tables_added"], res["subs_created"])
                for err in res.get("errors") or []:
                    log.warning("cluster db reconcile %s/%s: %s",
                                project.name, env.name, err)
            except Exception:  # noqa: BLE001
                log.exception("cluster db reconcile failed for %s", stack)


async def fanout_deploy(project_name: str, env_name: str, commit_sha: str | None,
                        force: bool = False) -> None:
    """After a successful LOCAL deploy, tell every peer to deploy the same
    (project, env). Peers pull config from us first, so env-var/domain changes
    ride along. `force` bypasses the peers' same-sha dedupe — used when the
    platform changed the stack without a new commit (cluster DB transition).
    Fire-and-forget per peer; own session (called from deploy.py's
    background task)."""
    async with SessionLocal() as session:
        state = await load_cluster(session)
        if not state or not state.get("initial_sync_done"):
            return
        node_id = await get_node_id(session)
        secret = cluster_secret(state)
        my_url = state.get("peer_url") or ""
        for peer in peers(state, node_id):
            if not peer.get("peer_url"):
                continue
            try:
                await peer_request(
                    "POST", peer["peer_url"], "/peer/deploy",
                    secret=secret, self_node_id=node_id,
                    body={"project_name": project_name, "env_name": env_name,
                          "commit_sha": commit_sha, "source_peer_url": my_url,
                          "force": force},
                    timeout=20,
                )
                log.info("fanout: %s/%s dispatched to %s", project_name, env_name, peer["peer_url"])
            except PeerError as e:
                log.warning("fanout to %s failed (its reconcile loop will catch up): %s",
                            peer["peer_url"], e)


async def check_network_conflicts(session: AsyncSession, state: dict[str, Any]) -> list[str]:
    """Detect docker networks whose subnet shadows a cluster peer address —
    containers would route peer traffic into the bridge void instead of the
    LAN (exactly how an auto-allocated 192.168.0.0/20 network once broke
    replication). Logged loudly + persisted for the UI."""
    import ipaddress
    from .host import _docker_request
    node_id = await get_node_id(session)
    peer_ips = []
    for n in state.get("roster") or []:
        url = (n.get("peer_url") or "").split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
        if url and n.get("node_id") != node_id:
            try:
                peer_ips.append(ipaddress.ip_address(url))
            except ValueError:
                continue
    if not peer_ips:
        return []
    conflicts: list[str] = []
    try:
        code, body = await asyncio.to_thread(_docker_request, "GET", "/networks")
        nets = json.loads(body[body.find(b"["):body.rfind(b"]") + 1]) if code == 200 else []
    except Exception:  # noqa: BLE001
        return []
    for net in nets:
        for cfg in ((net.get("IPAM") or {}).get("Config") or []):
            subnet = cfg.get("Subnet")
            if not subnet:
                continue
            try:
                network = ipaddress.ip_network(subnet, strict=False)
            except ValueError:
                continue
            hit = [str(ip) for ip in peer_ips if ip in network]
            if hit:
                msg = (f"docker network '{net.get('Name')}' ({subnet}) shadows cluster "
                       f"peer(s) {', '.join(hit)} — containers cannot reach them. "
                       f"Recreate that network on another subnet (set docker "
                       f"default-address-pools away from your LAN range).")
                conflicts.append(msg)
                log.error("NETWORK CONFLICT: %s", msg)
    await _set_setting(session, "network_conflicts", {
        "checked_at": datetime.utcnow().isoformat(), "conflicts": conflicts,
    })
    return conflicts


async def cluster_loop() -> None:
    cycle = 0
    while True:
        try:
            async with SessionLocal() as session:
                state = await load_cluster(session)
                if state:
                    # Self-heal the on-disk key override: anything that
                    # recreates /opt/homebox/admin (deploy rsync, restore)
                    # must not silently drop the cluster keys.
                    if not CLUSTER_KEYS_FILE.exists():
                        log.warning("cluster-keys.json missing — rewriting from live settings")
                        _write_cluster_keys(settings.encryption_key, settings.app_secret)
                    try:
                        state = await _heartbeat(session, state)
                    except ControlPlaneError as e:
                        log.warning("heartbeat failed (data plane unaffected): %s", e)
                    if not state.get("initial_sync_done"):
                        await initial_sync(session, state)
                    elif cycle % RECONCILE_EVERY == 0:
                        await reconcile_from_peers(session, state)
                        await ensure_db_replication(session, state)
                        # Keep the admin/peer Traefik routes current (cheap,
                        # idempotent — heals a route file written before
                        # admin_domain was known).
                        await ensure_peer_route(session)
                        await check_network_conflicts(session, state)
                # Account link is independent of membership: keeps the
                # overview cache fresh and executes pending join directives
                # (an invite issued from another node).
                try:
                    overview = await account_poll(session)
                    if overview:
                        await _maybe_autojoin(session, overview)
                except ControlPlaneError as e:
                    log.warning("account poll failed: %s", e)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop must survive anything
            log.exception("cluster loop cycle failed")
        cycle += 1
        await asyncio.sleep(LOOP_INTERVAL)


def start() -> asyncio.Task:
    return asyncio.create_task(cluster_loop(), name="homebox-cluster")


async def stop(task: asyncio.Task) -> None:
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
