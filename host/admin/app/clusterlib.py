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
APP_SERVING_KEY = "app_serving"  # False → this node is drained of app traffic
MIRROR_PROMOTED_KEY = "mirror_promoted"  # True → a mirror node has auto-promoted to serving
MIRROR_CACHE_KEY = "mirror"      # cached CP mirror status (for cheap /api/cluster reads)

VERSION = "0.2-cluster"
LOOP_INTERVAL = 60          # heartbeat cadence (seconds)
RECONCILE_EVERY = 5         # deployment reconcile every N cycles
PEER_TOKEN_WINDOW = 600     # peer auth token validity (seconds)

# Mirror failover thresholds (in cluster_loop ticks ≈ LOOP_INTERVAL seconds).
# The slow loop is the BACKSTOP; when the node runs the fast probe loop
# (mirror_probe_loop, settings.mirror_probe_*) promotion normally happens there
# in seconds. Demotion stays here on purpose: it must be slow so a flapping
# home connection doesn't bounce traffic between the cluster and the mirror.
MIRROR_PROMOTE_TICKS = 3    # ~180s of no healthy serving peer → promote
MIRROR_DEMOTE_TICKS = 2     # ~120s of a healthy serving peer → demote

_license_state_logged: str | None = None  # last license-state string we logged


class FailoverCounter:
    """Consecutive-streak tracker shared by the slow tick and the fast probe
    loop. record() returns "promote", "demote", or None. In-memory only: a
    restart resets streaks, which only delays a decision by one window."""

    def __init__(self, promote_after: int, demote_after: int) -> None:
        self.promote_after = promote_after
        self.demote_after = demote_after
        self.unhealthy = 0
        self.healthy = 0

    def reset(self) -> None:
        self.unhealthy = 0
        self.healthy = 0

    def record(self, healthy: bool, promoted: bool) -> str | None:
        if healthy:
            self.healthy += 1
            self.unhealthy = 0
        else:
            self.unhealthy += 1
            self.healthy = 0
        if not promoted and not healthy and self.unhealthy >= self.promote_after:
            self.unhealthy = 0
            return "promote"
        if promoted and healthy and self.healthy >= self.demote_after:
            self.healthy = 0
            return "demote"
        return None


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


# ───── app-serving drain (reversible, control-plane-visible) ──────────────────


async def get_app_serving(session: AsyncSession) -> bool:
    """Whether this node currently serves app traffic. When False the node is
    'drained': its Cloudflare connector is down (so the shared tunnel routes app
    requests to peers) but the admin + cluster loop keep running, so it still
    heartbeats the control plane and can be re-enabled. Default True."""
    val = await _get_setting(session, APP_SERVING_KEY)
    return True if val is None else bool(val)


async def apply_app_serving(session: AsyncSession, serving: bool) -> dict[str, Any]:
    """Persist the desired serving state and enforce it now: stop the connector
    to drain, or relaunch it from the stored connector token to resume. The
    monitor loop re-enforces this each cycle, so a drained node stays drained
    (it won't be resurrected) and a resumed one is kept up."""
    from . import cloudflare as cf
    from .host import remove_container, run_cloudflared_remote
    await _set_setting(session, APP_SERVING_KEY, bool(serving))
    await session.commit()
    if not serving:
        remove_container("homebox-cloudflared")
        return {"serving": False, "connector": "stopped"}
    cf_state = await cf.load_state(session)
    token = cf.get_connector_token(cf_state)
    if not token:
        return {"serving": True, "connector": "no token (tunnel not configured)"}
    ok, msg = await asyncio.to_thread(run_cloudflared_remote, token)
    return {"serving": True, "connector": "started" if ok else f"start failed: {msg}"}


def cluster_display_name(name: Any) -> str:
    """Coalesce a missing/empty cluster name to the default "home". The control
    plane already does this at every emission point; this is the node-side
    defensive twin for older CPs (and for a local state blob saved before the
    default existed)."""
    return (str(name).strip() if name else "") or "home"


def roster_role(entry: dict[str, Any]) -> str:
    """This roster entry's cluster role, defaulting to 'peer' for pre-role
    control planes / older nodes that never advertised one."""
    role = (entry or {}).get("role")
    return role if role in ("peer", "mirror") else "peer"


async def serving_peers_excluding(
    session: AsyncSession, state: dict[str, Any], target_node_id: str
) -> list[dict[str, Any]]:
    """Cluster members OTHER than `target` CONFIRMED to be serving app traffic
    right now, each annotated {node_id, role}. Callers use this to refuse
    draining the LAST serving node — otherwise every connector goes down and the
    shared tunnel has nowhere to route.

    Self is judged by the authoritative local flag. Each PEER is checked LIVE
    over the peer API rather than trusting the roster: the roster's `serving`
    flag lags a heartbeat, and that stale window is exactly what let a rapid
    "disable both" slip past. A peer we can't reach doesn't count — refusing the
    drain is far safer than risking full downtime.

    Returns dicts (not bare ids) so the last-serving-node guards can tell a peer
    from a mirror: draining the last serving NON-MIRROR node is allowed when an
    online mirror is standing by to auto-promote."""
    self_id = await get_node_id(session)
    secret = cluster_secret(state)
    out: list[dict[str, Any]] = []
    for n in state.get("roster") or []:
        nid = n.get("node_id")
        if not nid or nid == target_node_id:
            continue
        role = roster_role(n)
        if nid == self_id:
            if await get_app_serving(session):
                out.append({"node_id": nid, "role": role})
            continue
        peer_url = n.get("peer_url")
        if not peer_url:
            continue
        try:
            resp = await peer_request(
                "GET", peer_url, "/peer/ping",
                secret=secret, self_node_id=self_id, timeout=8,
            )
        except PeerError:
            continue  # unreachable → can't confirm it's serving → don't count it
        if resp.get("serving") is not False:  # older peers omit the field → assume serving
            out.append({"node_id": nid, "role": resp.get("role") or role})
    return out


def _roster_fresh(entry: dict[str, Any]) -> bool:
    """Whether a roster entry looks online: the CP's `online` flag, or a
    last_seen within ~2 heartbeats when it's provided instead."""
    if entry.get("online"):
        return True
    last_seen = entry.get("last_seen")
    if not last_seen:
        return False
    from . import licenselib
    ts = licenselib._to_epoch(last_seen)
    return ts is not None and (time.time() - ts) < LOOP_INTERVAL * 2.5


async def online_mirror_standby(
    session: AsyncSession, state: dict[str, Any], target_node_id: str,
) -> bool:
    """True when the roster holds a non-evicted MIRROR (other than target) that
    is online — via a fresh roster entry or a live /peer/ping. Such a mirror
    will auto-promote, so draining the last serving non-mirror node is safe."""
    self_id = await get_node_id(session)
    secret = cluster_secret(state)
    for n in state.get("roster") or []:
        nid = n.get("node_id")
        if not nid or nid == target_node_id or roster_role(n) != "mirror":
            continue
        if _roster_fresh(n):
            return True
        peer_url = n.get("peer_url")
        if not peer_url or nid == self_id:
            continue
        try:
            await peer_request("GET", peer_url, "/peer/ping",
                               secret=secret, self_node_id=self_id, timeout=8)
            return True
        except PeerError:
            continue
    return False


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
    """A control-plane call failed. Carries the HTTP status (when there was a
    response) and the CP's human-readable detail so routes can propagate a clean
    402 (plan gating) or other status instead of collapsing everything to 500."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 detail: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.detail = detail or message


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
        raise ControlPlaneError(
            f"control plane: {detail} (HTTP {r.status_code})",
            status_code=r.status_code, detail=detail,
        )
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
    from . import meshlib
    _, wg_pub = await meshlib.get_wg_keys(session)
    resp = await _cp("POST", control_plane_url, "/v1/clusters",
                     token=account_token_plain,
                     body={"name": name, "node_id": node_id, "node_name": node_name,
                           "pubkey": pub, "peer_url": peer_url, "version": VERSION,
                           "wg_pubkey": wg_pub, "wg_port": settings.wg_advertise_port,
                           "role": settings.node_role})
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
    from . import licenselib
    await licenselib.record_license_verification(session, state, control_plane_url.rstrip("/"))
    await save_cluster(session, state)
    # Make the current keys explicit on disk so every future boot (and any
    # compose recreate with stale env) stays on the cluster keys.
    _write_cluster_keys(settings.encryption_key, settings.app_secret)
    await ensure_peer_route(session)
    await session.commit()
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
    from . import meshlib
    _, wg_pub = await meshlib.get_wg_keys(session)

    reg = await _cp("POST", control_plane_url, f"/v1/clusters/{cluster_id}/nodes",
                    body={"join_token": join_token, "node_id": node_id,
                          "node_name": node_name, "pubkey": pub,
                          "peer_url": peer_url, "version": VERSION,
                          "wg_pubkey": wg_pub, "wg_port": settings.wg_advertise_port,
                          "role": settings.node_role})

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
    from . import licenselib
    await licenselib.record_license_verification(session, state, control_plane_url.rstrip("/"))
    await save_cluster(session, state)
    _write_cluster_keys(new_enc, new_app)
    # A mirror node runs drained (standby): it stays hot via replication + peer
    # deploys but never serves app traffic until it auto-promotes on failover.
    if settings.node_role == "mirror":
        await _set_setting(session, APP_SERVING_KEY, False)
        await _set_setting(session, MIRROR_PROMOTED_KEY, False)
    await ensure_peer_route(session)
    # get_session never commits — without this the membership blob rolls back
    # at request close and the restart comes up standalone.
    await session.commit()
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


# ───── stored provider tokens (silent account auth, G4) ──────────────────────
#
# Every successful OAuth flow that yields a provider access token persists it
# ENCRYPTED under the "provider_tokens" setting, keyed "<provider>:<email>":
#   {"github:al@example.com": {"token_encrypted": ..., "saved_at": iso}}
# Node-local by design: the key is NOT in cluster_sync.CLUSTER_SETTINGS, so it
# never rides peer sync or the account vault. No API ever returns the token.

PROVIDER_TOKENS_KEY = "provider_tokens"


async def load_provider_tokens(session: AsyncSession) -> dict[str, dict[str, Any]]:
    val = await _get_setting(session, PROVIDER_TOKENS_KEY)
    return dict(val) if isinstance(val, dict) else {}


async def save_provider_token(
    session: AsyncSession, provider: str, email: str, access_token: str,
) -> None:
    """Upsert one provider token (encrypted at rest). Commits."""
    if not (provider and email and access_token):
        return
    tokens = await load_provider_tokens(session)
    tokens[f"{provider.strip().lower()}:{email.strip().lower()}"] = {
        "token_encrypted": crypto.encrypt(access_token),
        "saved_at": datetime.utcnow().isoformat(),
    }
    await _set_setting(session, PROVIDER_TOKENS_KEY, tokens)
    await session.commit()


async def delete_provider_token(session: AsyncSession, key: str) -> None:
    tokens = await load_provider_tokens(session)
    if key in tokens:
        tokens.pop(key, None)
        await _set_setting(session, PROVIDER_TOKENS_KEY, tokens)
        await session.commit()


def freshest_provider_tokens(
    tokens: dict[str, dict[str, Any]], provider: str | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Usable stored tokens, freshest first, optionally filtered by provider."""
    items = [
        (k, v) for k, v in tokens.items()
        if isinstance(v, dict) and v.get("token_encrypted")
    ]
    if provider:
        want = provider.strip().lower()
        items = [(k, v) for k, v in items if k.split(":", 1)[0] == want]
    return sorted(items, key=lambda kv: str(kv[1].get("saved_at") or ""), reverse=True)


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
    # Persist the link before the best-effort extras below — a failure in the
    # poll/backup must not roll back a link the control plane already recorded.
    await session.commit()
    try:
        await account_poll(session, blob)
    except ControlPlaneError as e:
        log.warning("initial account poll failed (overview fills in next cycle): %s", e)
    # Seed the cloud metadata backup right away so the UI shows a backup
    # timestamp without waiting for the loop's next reconcile cycle.
    try:
        from . import backuplib
        await backuplib.push_backup_if_changed(session)
    except Exception:  # noqa: BLE001 — the link itself already succeeded
        log.exception("initial cloud backup push failed")
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
    await session.commit()


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
    # Commit here, not in callers: the loop's steady-state cycles have no other
    # commit, so the registry/online cache would silently roll back every time.
    await session.commit()
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


# ───── typed remote-op directives (poll channel, D5) ─────────────────────────
#
# The poll response's new-style "directives" list carries remote operations
# queued from the god view / portal: [{id, type, payload}]. The legacy join
# "directive" field is unchanged (_maybe_autojoin above). Each directive is
# executed locally via the existing flows and acked to the CP; results are
# remembered in-process so a re-delivered directive (ack lost) is re-acked,
# never re-executed.

_handled_directives: dict[str, tuple[str, str]] = {}  # id -> (status, detail)
_HANDLED_DIRECTIVES_CAP = 500


async def _execute_split_cluster(session: AsyncSession, payload: dict[str, Any]) -> tuple[str, str]:
    """v1 split-cluster: the directive's RECIPIENT splits off to found the new
    cluster (directives are point-to-point — an ordinal election here could
    wait on a node that never received it), then invites the other listed
    nodes (they join via the CP's normal join directives)."""
    node_ids = [str(n) for n in (payload.get("node_ids") or []) if n]
    name = (payload.get("name") or "").strip()
    if not node_ids or not name:
        return ("error", "split_cluster requires node_ids and name")
    self_id = await get_node_id(session)
    if self_id not in node_ids:
        return ("error", "this node is not in the split set — send the directive to a member of it")
    state = await load_cluster(session)
    if not state:
        return ("error", "this node is not in a cluster")
    acct = await load_account(session)
    acct_token = crypto.decrypt(acct["token_encrypted"]) if acct else ""
    cp_url = (acct or {}).get("control_plane_url") or state["control_plane_url"]
    res = await split_off_flow(session, name=name)
    new_id = res["cluster_id"]
    invited: list[str] = []
    errors: list[str] = []
    for nid in node_ids:
        if nid == self_id:
            continue
        try:
            await _cp("POST", cp_url, f"/v1/clusters/{new_id}/invite",
                      token=acct_token, body={"node_id": nid})
            invited.append(nid)
        except ControlPlaneError as e:
            errors.append(f"{nid}: {e.detail}")
    detail = f"founded cluster {new_id} ({res['name']}); invited: {', '.join(invited) or 'none'}"
    if errors:
        return ("error", detail + "; invite errors: " + "; ".join(errors))
    return ("done", detail)


async def _execute_directive(session: AsyncSession, d: dict[str, Any]) -> tuple[str, str]:
    """Run one typed directive; returns (status, detail) for the ack."""
    dtype = d.get("type")
    payload = d.get("payload") or {}
    node_id = await get_node_id(session)

    if dtype == "set_serving":
        serving = bool(payload.get("serving"))
        if not serving:
            # Same last-serving-node guard as the UI route: never let the
            # cluster (or a mirror-less roster) lose ALL app ingress.
            state = await load_cluster(session)
            if state and not await serving_peers_excluding(session, state, node_id):
                if not await online_mirror_standby(session, state, node_id):
                    return ("error",
                            "refused: this is the last serving node — at least one "
                            "node must keep serving app traffic (enable another node "
                            "or add an online mirror first)")
        result = await apply_app_serving(session, serving)
        return ("done", f"serving={result.get('serving')} connector={result.get('connector', '-')}")

    if dtype == "split_off":
        state = await load_cluster(session)
        acct = await load_account(session)
        default_name = ((state or {}).get("node_name")
                        or (acct or {}).get("node_name") or "node")
        name = (payload.get("name") or "").strip() or f"{default_name}-cluster"
        res = await split_off_flow(session, name=name)
        return ("done", f"split off into cluster {res['cluster_id']} ({res['name']})")

    if dtype == "split_cluster":
        return await _execute_split_cluster(session, payload)

    if dtype == "join":
        # New-style join (typed) — same semantics as the legacy field.
        if await load_cluster(session):
            return ("error", "already in a cluster — leave it first")
        if not payload.get("join_token"):
            return ("error", "join directive missing join_token")
        acct = await load_account(session)
        if not acct:
            return ("error", "no account link on this node")
        await join_cluster_flow(
            session,
            control_plane_url=acct["control_plane_url"],
            join_token=payload["join_token"],
            peer_url=acct.get("peer_url") or "",
            node_name=acct.get("node_name") or "",
        )
        return ("done", "joined cluster")

    return ("error", f"unknown directive type {dtype!r}")


async def handle_directives(session: AsyncSession, overview: dict[str, Any]) -> None:
    """Execute + ack every typed directive in a poll response. Idempotent per
    directive id; a bad directive never crashes the loop — it's acked as an
    error and the loop moves on."""
    directives = overview.get("directives")
    if not isinstance(directives, list) or not directives:
        return
    acct = await load_account(session)
    if not acct:
        return
    cp_url = acct["control_plane_url"]
    token = crypto.decrypt(acct["token_encrypted"])
    for d in directives:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        did = str(d["id"])
        if did in _handled_directives:
            status, detail = _handled_directives[did]  # re-ack only, never re-run
        else:
            try:
                status, detail = await _execute_directive(session, d)
            except (ControlPlaneError, PeerError, ValueError) as e:
                status, detail = "error", str(e)
            except Exception as e:  # noqa: BLE001 — never let one directive kill the loop
                log.exception("directive %s (%s) failed", did, d.get("type"))
                status, detail = "error", str(e)
            if len(_handled_directives) >= _HANDLED_DIRECTIVES_CAP:
                for k in list(_handled_directives)[:_HANDLED_DIRECTIVES_CAP // 2]:
                    _handled_directives.pop(k, None)
            _handled_directives[did] = (status, detail)
            log.info("directive %s (%s): %s — %s", did, d.get("type"), status, detail)
        try:
            await _cp("POST", cp_url, f"/v1/accounts/directives/{did}/ack",
                      token=token, body={"status": status, "detail": detail})
        except ControlPlaneError as e:
            log.warning("directive ack failed for %s (will re-ack next poll): %s", did, e)


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
    await session.commit()
    log.info("left cluster %s: %s", state["cluster_id"], result)
    return result


async def split_off_flow(
    session: AsyncSession, *, name: str, peer_url: str | None = None,
) -> dict[str, Any]:
    """Leave the current cluster and immediately found a NEW one with this node
    as the seed, keeping its data and keys. create_cluster_flow adopts the
    node's current ENCRYPTION_KEY/APP_SECRET, so everything encrypted locally
    stays decryptable — that's the point of split vs leave+fresh-create.

    Stacks are never torn down. The shared-tunnel connector is stopped only
    when OTHER nodes remain in the roster to serve it — a lone node splitting
    off keeps its connector (there's nobody else to route to). The homebox.sh
    account link is untouched. The leave half is best-effort (same tolerance as
    leave_cluster_flow); control-plane errors from the create half propagate
    (esp. 402 plan gating, which routes surface to the UI)."""
    state = await load_cluster(session)
    if not state:
        raise ValueError("This node is not part of a cluster.")
    acct_token = account_token(state)
    if not acct_token:
        blob = await load_account(session)
        acct_token = crypto.decrypt(blob["token_encrypted"]) if blob else ""
    if not acct_token:
        raise ValueError(
            "No account credential on this node — link a homebox.sh account "
            "(or split off from the founding node)."
        )
    # Capture identity BEFORE leaving (leave clears the cluster blob).
    node_id = await get_node_id(session)
    control_plane_url = state["control_plane_url"]
    node_name = state.get("node_name") or ""
    new_peer_url = (peer_url or state.get("peer_url") or "").strip()
    has_other_nodes = bool(peers(state, node_id))

    # Pre-flight the plan gate BEFORE the destructive leave half: peers drop
    # their replication to us during leave, so a 402 on the create half would
    # otherwise strand the node half-out of its cluster. An unreachable/old
    # control plane skips the check — the create call still enforces.
    try:
        me = await _cp("GET", control_plane_url, "/v1/accounts/me", token=acct_token)
    except ControlPlaneError:
        me = None
    if me is not None and isinstance(me.get("features"), list) and "cluster" not in me["features"]:
        raise ControlPlaneError(
            "plan gate: cluster feature missing",
            status_code=402,
            detail="Your plan doesn't include clustering — upgrade at homebox.sh/cloud to split off into a new cluster.",
        )

    await leave_cluster_flow(session, stop_tunnel=has_other_nodes, teardown_stacks=False)
    new_state = await create_cluster_flow(
        session,
        control_plane_url=control_plane_url,
        account_token_plain=acct_token,
        name=name,
        peer_url=new_peer_url,
        node_name=node_name,
    )
    await session.commit()
    log.info("split off into new cluster %s (%s)", new_state["cluster_id"], new_state["name"])
    return {"cluster_id": new_state["cluster_id"], "name": new_state["name"]}


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
    await session.commit()
    # Clean up our side right away rather than waiting for the next cycle.
    await ensure_db_replication(session, state)
    return resp


# ───── steady-state loop ──────────────────────────────────────────────────────


async def _heartbeat(session: AsyncSession, state: dict[str, Any]) -> dict[str, Any]:
    node_id = await get_node_id(session)
    from . import meshlib
    _, wg_pub = await meshlib.get_wg_keys(session)
    resp = await _cp(
        "POST", state["control_plane_url"],
        f"/v1/clusters/{state['cluster_id']}/heartbeat",
        token=node_token(state),
        body={"node_id": node_id, "peer_url": state.get("peer_url") or "",
              "version": VERSION, "name": state.get("node_name") or "",
              "wg_pubkey": wg_pub, "wg_port": settings.wg_advertise_port,
              "serving": await get_app_serving(session)},
    )
    state["roster"] = resp["nodes"]
    state["license"] = resp.get("license")
    from . import licenselib
    await licenselib.record_license_verification(session, state, state["control_plane_url"])
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
                # Cloud DB VMs replicating this stack's database join as extra
                # Spock nodes; the coordinator also wires the VM's own subs.
                from . import targetslib
                extra_nodes = await targetslib.db_vm_extra_nodes(
                    session, project, env, info["service"])
                res = await cluster_db.ensure_replication(
                    stack=stack, info=info, state=state, self_node_id=node_id,
                    extra_nodes=extra_nodes,
                    wire_extra=await targetslib.is_cloud_coordinator(session, state),
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
        # License gate (gentle): an expired license (beyond grace) stops NEW
        # fan-out but never touches what's already running. A mirror never
        # originates deploys.
        from . import licenselib
        if licenselib.license_status(state).get("expired"):
            log.warning("fanout skipped: license expired (beyond grace) — existing deploys untouched")
            return
        if settings.node_role == "mirror":
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


# ───── mirror mode (standby node with auto-failover) ─────────────────────────


async def _healthy_serving_nonmirror_peer(
    session: AsyncSession, state: dict[str, Any],
) -> bool:
    """True when at least one NON-MIRROR peer is healthy: a live /peer/ping
    succeeds AND it reports serving=True. Unreachable ⇒ not healthy (that's what
    lets a mirror promote when the peers are actually down). Falls back to a
    fresh roster entry that shows serving only when a live check isn't possible
    to attempt (no peer_url)."""
    self_id = await get_node_id(session)
    secret = cluster_secret(state)
    for n in state.get("roster") or []:
        nid = n.get("node_id")
        if not nid or nid == self_id or roster_role(n) != "peer":
            continue
        peer_url = n.get("peer_url")
        if peer_url:
            try:
                resp = await peer_request("GET", peer_url, "/peer/ping",
                                          secret=secret, self_node_id=self_id, timeout=8)
            except PeerError:
                continue  # unreachable → not healthy
            if resp.get("serving") is not False:
                return True
            continue
        # No peer_url to ping: trust a fresh roster entry that still shows serving.
        if _roster_fresh(n) and n.get("serving") is not False:
            return True
    return False


async def promote_mirror(session: AsyncSession) -> None:
    """Promote this mirror to serving. In cold-apps mode the app containers are
    started FIRST (they're created-but-stopped while drained) so they're coming
    up while the connector registers — the edge only routes here once the
    connector is live."""
    if settings.mirror_cold_apps:
        from .host import start_app_containers
        started = await asyncio.to_thread(start_app_containers)
        if started:
            log.warning("MIRROR: started %d cold app container(s)", started)
    await apply_app_serving(session, True)
    await _set_setting(session, MIRROR_PROMOTED_KEY, True)
    await session.commit()


async def demote_mirror(session: AsyncSession) -> None:
    """Return this mirror to drained standby; in cold-apps mode also stop the
    app containers again (databases keep running — live Spock subscribers)."""
    await apply_app_serving(session, False)
    await _set_setting(session, MIRROR_PROMOTED_KEY, False)
    await session.commit()
    if settings.mirror_cold_apps:
        from .host import stop_app_containers
        stopped = await asyncio.to_thread(stop_app_containers)
        if stopped:
            log.info("MIRROR: stopped %d app container(s) (cold standby)", stopped)


_slow_counter = FailoverCounter(MIRROR_PROMOTE_TICKS, MIRROR_DEMOTE_TICKS)


async def mirror_failover_tick(session: AsyncSession, state: dict[str, Any]) -> None:
    """One failover evaluation for a mirror node (called each cluster_loop cycle
    when role==mirror). Promotes to serving after MIRROR_PROMOTE_TICKS
    consecutive cycles with NO healthy serving non-mirror peer, and demotes back
    to standby after MIRROR_DEMOTE_TICKS consecutive cycles once a healthy
    serving peer reappears. Counters live in module memory; the persisted
    `mirror_promoted` flag keeps a restarted-while-promoted mirror serving until
    demote conditions are met. Promotion normally lands earlier via the fast
    probe loop; this tick is the backstop and owns demotion."""
    healthy = await _healthy_serving_nonmirror_peer(session, state)
    promoted = bool(await _get_setting(session, MIRROR_PROMOTED_KEY))

    action = _slow_counter.record(healthy, promoted)
    if action == "promote":
        log.error(
            "MIRROR FAILOVER: no healthy serving peer for %d cycles — PROMOTING "
            "this mirror to serve app traffic", MIRROR_PROMOTE_TICKS,
        )
        await promote_mirror(session)
    elif action == "demote":
        log.warning(
            "MIRROR: healthy serving peer back for %d cycles — DEMOTING this "
            "mirror to standby", MIRROR_DEMOTE_TICKS,
        )
        await demote_mirror(session)


# ───── mirror fast probe (sub-10s failover detection) ─────────────────────────

MIRROR_PROBE_TIMEOUT = 2.0  # per-peer ping timeout in the fast loop (seconds)


async def _fast_probe_healthy(
    session: AsyncSession, state: dict[str, Any], timeout: float,
) -> bool:
    """Concurrent, short-timeout version of _healthy_serving_nonmirror_peer for
    the fast probe loop: ping every non-mirror peer at once instead of walking
    them serially with an 8s timeout (which would make a down-cluster probe
    take N×8s and defeat the fast cadence)."""
    self_id = await get_node_id(session)
    secret = cluster_secret(state)
    candidates: list[dict[str, Any]] = []
    fallback = False
    for n in state.get("roster") or []:
        nid = n.get("node_id")
        if not nid or nid == self_id or roster_role(n) != "peer":
            continue
        if n.get("peer_url"):
            candidates.append(n)
        elif _roster_fresh(n) and n.get("serving") is not False:
            # No peer_url to ping: trust a fresh roster entry that shows serving.
            fallback = True

    async def ping(n: dict[str, Any]) -> bool:
        try:
            resp = await peer_request("GET", n["peer_url"], "/peer/ping",
                                      secret=secret, self_node_id=self_id,
                                      timeout=timeout)
        except PeerError:
            return False
        return resp.get("serving") is not False

    if candidates:
        results = await asyncio.gather(*(ping(n) for n in candidates))
        if any(results):
            return True
    return fallback


async def mirror_probe_loop() -> None:
    """Fast failover detector for mirror nodes. Probes serving peers every
    settings.mirror_probe_interval seconds and promotes after
    settings.mirror_probe_failures consecutive all-down probes — roughly
    interval×failures + one timeout of detection latency (~8-10s at the
    defaults) versus the slow tick's ~180s. Promotion only: demotion stays on
    the slow tick so a flapping uplink can't bounce traffic."""
    interval = float(settings.mirror_probe_interval)
    failures = int(settings.mirror_probe_failures)
    counter = FailoverCounter(promote_after=failures, demote_after=1 << 30)
    log.info("mirror fast-probe loop started (interval=%.1fs, failures=%d)",
             interval, failures)
    while True:
        try:
            async with SessionLocal() as session:
                state = await load_cluster(session)
                if state and state.get("initial_sync_done"):
                    promoted = bool(await _get_setting(session, MIRROR_PROMOTED_KEY))
                    if promoted:
                        counter.reset()
                    else:
                        healthy = await _fast_probe_healthy(
                            session, state,
                            timeout=max(MIRROR_PROBE_TIMEOUT, interval))
                        if counter.record(healthy, promoted) == "promote":
                            log.error(
                                "MIRROR FAILOVER (fast probe): no healthy serving "
                                "peer after %d consecutive probes — PROMOTING "
                                "this mirror to serve app traffic", failures,
                            )
                            await promote_mirror(session)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — the loop must survive anything
            log.exception("mirror probe tick failed")
        await asyncio.sleep(interval)


def start_mirror_probe() -> asyncio.Task:
    return asyncio.create_task(mirror_probe_loop(), name="homebox-mirror-probe")


async def refresh_mirror_status(session: AsyncSession, state: dict[str, Any]) -> dict[str, Any] | None:
    """Refresh the cached cloud-mirror status from the control plane (account
    token) and stash it under state["mirror"] so GET /api/cluster can serve it
    cheaply. Best-effort; returns the status dict or None."""
    acct = account_token(state)
    if not acct:
        blob = await load_account(session)
        acct = crypto.decrypt(blob["token_encrypted"]) if blob else ""
    if not acct or not state.get("cluster_id"):
        return None
    try:
        status = await _cp("GET", state["control_plane_url"],
                           f"/v1/clusters/{state['cluster_id']}/mirror", token=acct)
    except ControlPlaneError as e:
        log.warning("mirror status refresh failed: %s", e)
        return None
    state[MIRROR_CACHE_KEY] = status
    await save_cluster(session, state)
    return status


def _log_license_state_once(state: dict[str, Any]) -> None:
    """Log a license-state transition (expired / in-grace / ok) at most once per
    change, so the loop doesn't spam every cycle."""
    global _license_state_logged
    from . import licenselib
    st = licenselib.license_status(state)
    if st["expired"]:
        tag = "expired"
    elif st["in_grace"]:
        tag = "grace"
    else:
        tag = "ok"
    if tag == _license_state_logged:
        return
    _license_state_logged = tag
    if tag == "expired":
        log.error("LICENSE EXPIRED (beyond %s-day grace): new deploys and new DB "
                  "subscriptions are paused; running apps and the mesh are untouched. "
                  "Renew at your homebox.sh account.",
                  (state.get("license") or {}).get("grace_days", licenselib.GRACE_DAYS_DEFAULT))
    elif tag == "grace":
        log.warning("LICENSE in grace period (expired but within grace) — renew soon "
                    "to avoid pausing new deploys.")


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
                    # License gate (gentle): an expired license (beyond grace)
                    # pauses NEW Spock subscriptions; existing ones and running
                    # apps are never touched, and the mesh is never torn down.
                    from . import licenselib
                    _log_license_state_once(state)
                    lic_expired = licenselib.license_status(state).get("expired")
                    if not state.get("initial_sync_done"):
                        await initial_sync(session, state)
                    elif cycle % RECONCILE_EVERY == 0:
                        await reconcile_from_peers(session, state)
                        if not lic_expired:
                            await ensure_db_replication(session, state)
                        else:
                            log.warning("license expired — skipping new DB subscription "
                                        "creation (existing replication untouched)")
                        # Keep the admin/peer Traefik routes current (cheap,
                        # idempotent — heals a route file written before
                        # admin_domain was known).
                        await ensure_peer_route(session)
                        await check_network_conflicts(session, state)
                        # Refresh the cached cloud-mirror status for cheap
                        # /api/cluster reads (best-effort).
                        await refresh_mirror_status(session, state)
                    # WireGuard mesh: reconcile every cycle (cheap; peer
                    # endpoints/keys change as the roster does). Best-effort.
                    if state.get("initial_sync_done"):
                        try:
                            from . import meshlib
                            await meshlib.ensure_mesh(session, state)
                        except Exception:  # noqa: BLE001
                            log.exception("mesh reconcile failed")
                    # Mirror node: evaluate auto-failover every cycle.
                    if settings.node_role == "mirror" and state.get("initial_sync_done"):
                        try:
                            await mirror_failover_tick(session, state)
                        except Exception:  # noqa: BLE001
                            log.exception("mirror failover tick failed")
                # Account link is independent of membership: keeps the
                # overview cache fresh and executes pending join directives
                # (an invite issued from another node).
                try:
                    overview = await account_poll(session)
                    if overview:
                        await _maybe_autojoin(session, overview)
                        # Typed remote-op directives (set_serving/split_off/
                        # split_cluster/join) ride the same poll response.
                        try:
                            await handle_directives(session, overview)
                        except Exception:  # noqa: BLE001
                            log.exception("directive handling failed")
                except ControlPlaneError as e:
                    log.warning("account poll failed: %s", e)
                # Cloud metadata backup: for account-linked nodes (clustered or
                # standalone), on the same cadence as the reconcile work.
                # Deduped by content hash inside push_backup_if_changed.
                if cycle % RECONCILE_EVERY == 0:
                    try:
                        from . import backuplib
                        await backuplib.push_backup_if_changed(session)
                    except Exception:  # noqa: BLE001
                        log.exception("cloud backup push failed")
                    # Account metadata vault: pull→merge→push, coordinator (or
                    # standalone linked node) only. vault_tick records its own
                    # errors on the vault_state setting; this is the backstop.
                    try:
                        from . import vaultlib
                        await vaultlib.vault_tick(session)
                    except Exception:  # noqa: BLE001
                        log.exception("vault tick failed")
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
