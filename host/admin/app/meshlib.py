"""WireGuard mesh for cross-network clusters.

Each node runs a `homebox-mesh` container (host networking, NET_ADMIN,
/dev/net/tun) holding a wg0 interface with a deterministic overlay address
(10.77.x.y derived from the node's permanent ordinal). Peers are configured
from the control-plane roster (their wg pubkey + endpoint). Once up, the host
routes 10.77.0.0/16 through wg0, so replication and the peer API can target
peers' overlay IPs regardless of NAT.

Endpoint selection: nodes behind the SAME NAT (the control plane sees the same
public IP for both) reach each other on the LAN — use the LAN address from
peer_url. Otherwise use the peer's observed public IP. WireGuard is roaming, so
only one side needs a reachable endpoint for the tunnel to establish.

The image is built from an admin-container-local context (no host-path bind, so
it works identically on WSL2 and macOS Docker Desktop); the config is delivered
with `docker exec` rather than a bind mount for the same reason.
"""

import asyncio
import logging
import time
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from . import crypto
from .deploy import _run
from .host import remove_container, container_status

log = logging.getLogger("homebox.mesh")

MESH_CONTAINER = "homebox-mesh"
MESH_IMAGE = "homebox-mesh:latest"
WG_PORT = 51820
WG_KEYS_KEY = "wg_keys"
# A tunnel whose last handshake is within this window is treated as up. wg
# rekeys well inside 180s (handshakes ~every 120s under keepalive), so a peer
# that's genuinely reachable stays flagged; one that dropped ages out quickly.
HANDSHAKE_FRESH_S = 180

_DOCKERFILE = """FROM alpine:3.20
RUN apk add --no-cache wireguard-tools iproute2
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
"""

_ENTRYPOINT = """#!/bin/sh
set -u
CONF=/etc/wireguard/wg0.conf
up() {
  [ -f "$CONF" ] || return 0
  if ip link show wg0 >/dev/null 2>&1; then
    if wg-quick strip wg0 > /tmp/wg0.stripped 2>/dev/null; then
      wg syncconf wg0 /tmp/wg0.stripped 2>/dev/null || { wg-quick down wg0 2>/dev/null; wg-quick up wg0 2>/dev/null; }
    fi
  else
    wg-quick up wg0 2>/dev/null || true
  fi
}
trap 'wg-quick down wg0 2>/dev/null; exit 0' TERM INT
# Best-effort: enable forwarding so the host routes overlay traffic between
# bridge containers and wg0 (host usually has this on already).
sysctl -w net.ipv4.ip_forward=1 2>/dev/null || echo 1 > /proc/sys/net/ipv4/ip_forward 2>/dev/null || true
up
while true; do sleep 10; up; done
"""


def mesh_ip(ordinal: int) -> str:
    return f"10.77.{(ordinal >> 8) & 0xff}.{ordinal & 0xff}"


async def mesh_up_ordinals(state: dict) -> set[int]:
    """Ordinals of peers whose WireGuard tunnel is currently established (a
    handshake within HANDSHAKE_FRESH_S). The data plane prefers a peer's overlay
    IP when its tunnel is up and falls back to its LAN/public address otherwise,
    so this returns an empty set — meaning "use the direct address" — whenever
    the mesh isn't running (single-node, or an environment where the datapath
    can't form), keeping single-network clusters working unchanged."""
    if not container_status(MESH_CONTAINER).get("running"):
        return set()
    code, out = await _run(
        ["docker", "exec", MESH_CONTAINER, "wg", "show", "wg0", "latest-handshakes"],
        timeout=15,
    )
    if code != 0 or not out:
        return set()
    handshakes: dict[str, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            try:
                handshakes[parts[0]] = int(parts[1])
            except ValueError:
                continue
    now = time.time()
    up: set[int] = set()
    for n in state.get("roster") or []:
        pub, ordinal = n.get("wg_pubkey"), n.get("ordinal")
        if pub and ordinal:
            ts = handshakes.get(pub, 0)
            if ts and (now - ts) < HANDSHAKE_FRESH_S:
                up.add(int(ordinal))
    return up


async def get_wg_keys(session: AsyncSession) -> tuple[str, str]:
    """(private_b64, public_b64) — generated on first use, stored in settings."""
    from .clusterlib import _get_setting, _set_setting
    val = await _get_setting(session, WG_KEYS_KEY)
    if isinstance(val, dict) and val.get("private") and val.get("public"):
        return val["private"], val["public"]
    priv, pub = crypto.generate_wg_keypair()
    await _set_setting(session, WG_KEYS_KEY, {"private": priv, "public": pub})
    return priv, pub


def _lan_host(peer_url: str) -> str:
    return (peer_url or "").split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]


def _peer_endpoint(peer: dict, my_public_ip: str) -> str | None:
    """Where to reach a peer's wg. Same NAT (same public IP) → its LAN address;
    else its observed public IP. None when we can't determine one (the peer
    will initiate to us instead)."""
    port = peer.get("wg_port") or WG_PORT
    peer_public = peer.get("public_ip") or ""
    if my_public_ip and peer_public and my_public_ip == peer_public:
        host = _lan_host(peer.get("peer_url") or "")
        return f"{host}:{port}" if host else None
    if peer_public:
        return f"{peer_public}:{port}"
    return None


def build_conf(*, priv: str, my_ordinal: int, peers: list[dict], my_public_ip: str) -> str:
    lines = [
        "[Interface]",
        f"PrivateKey = {priv}",
        f"Address = {mesh_ip(my_ordinal)}/16",
        f"ListenPort = {WG_PORT}",
        "",
    ]
    for p in peers:
        wg_pub = p.get("wg_pubkey")
        ordinal = p.get("ordinal")
        if not wg_pub or not ordinal:
            continue
        lines.append("[Peer]")
        lines.append(f"PublicKey = {wg_pub}")
        lines.append(f"AllowedIPs = {mesh_ip(ordinal)}/32")
        ep = _peer_endpoint(p, my_public_ip)
        if ep:
            lines.append(f"Endpoint = {ep}")
        lines.append("PersistentKeepalive = 25")
        lines.append("")
    return "\n".join(lines) + "\n"


async def _image_exists() -> bool:
    code, _ = await _run(["docker", "image", "inspect", MESH_IMAGE], timeout=20)
    return code == 0


async def _build_image() -> bool:
    """Build the mesh image from an admin-container-local context so there's no
    host-path dependency (works on WSL2 and macOS alike)."""
    ctx = Path("/tmp/homebox-mesh-build")
    ctx.mkdir(parents=True, exist_ok=True)
    (ctx / "Dockerfile").write_text(_DOCKERFILE)
    (ctx / "entrypoint.sh").write_text(_ENTRYPOINT)
    code, out = await _run(["docker", "build", "-t", MESH_IMAGE, str(ctx)], timeout=300)
    if code:
        log.error("mesh image build failed: %s", out[-500:])
        return False
    return True


async def _run_container() -> tuple[bool, str]:
    """(Re)create the host-network mesh container."""
    from .host import _docker_request_json, _docker_request
    from urllib.parse import quote
    remove_container(MESH_CONTAINER)
    payload = {
        "Image": MESH_IMAGE,
        "HostConfig": {
            "NetworkMode": "host",
            "CapAdd": ["NET_ADMIN", "SYS_MODULE"],
            "Devices": [{"PathOnHost": "/dev/net/tun", "PathInContainer": "/dev/net/tun",
                         "CgroupPermissions": "rwm"}],
            # ip_forward is a host-namespace sysctl — can't be set per-container
            # on a host-network container; the entrypoint enables it best-effort
            # and Docker usually has it on already.
            "RestartPolicy": {"Name": "unless-stopped"},
        },
    }
    code, body = await asyncio.to_thread(
        _docker_request_json, "POST", f"/containers/create?name={quote(MESH_CONTAINER)}", payload
    )
    if code not in (200, 201):
        return False, f"create failed: {code} {body[:200]!r}"
    code, body = await asyncio.to_thread(
        _docker_request, "POST", f"/containers/{quote(MESH_CONTAINER)}/start"
    )
    if code in (204, 304):
        return True, "started"
    return False, f"start failed: {code} {body[:200]!r}"


async def _write_conf(conf: str) -> bool:
    """Deliver wg0.conf into the running mesh container (no host-path bind)."""
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", "-i", MESH_CONTAINER, "sh", "-c",
        "mkdir -p /etc/wireguard && cat > /etc/wireguard/wg0.conf",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate(conf.encode())
    if proc.returncode:
        log.warning("mesh conf write failed: %s", out.decode("utf-8", "replace")[:200])
        return False
    # Nudge an immediate apply rather than waiting for the 10s loop.
    await _run(["docker", "exec", MESH_CONTAINER, "sh", "-c",
                "wg-quick strip wg0 > /tmp/s 2>/dev/null && wg syncconf wg0 /tmp/s 2>/dev/null || wg-quick up wg0 2>/dev/null || true"],
               timeout=20)
    return True


async def ensure_mesh(session: AsyncSession, state: dict) -> None:
    """Bring up / reconcile the WireGuard mesh from the current roster. Called
    from the cluster loop. Best-effort — a mesh failure never breaks the data
    plane (which still has its LAN path)."""
    from .clusterlib import get_node_id  # local import avoids cycle

    node_id = await get_node_id(session)
    roster = state.get("roster") or []
    me = next((n for n in roster if n.get("node_id") == node_id), None)
    if not me or not me.get("ordinal"):
        return
    peers = [n for n in roster if n.get("node_id") != node_id and n.get("wg_pubkey")]

    priv, _pub = await get_wg_keys(session)
    conf = build_conf(
        priv=priv, my_ordinal=me["ordinal"], peers=peers,
        my_public_ip=me.get("public_ip") or "",
    )

    if not await _image_exists():
        if not await _build_image():
            return
    st = container_status(MESH_CONTAINER)
    if not st.get("running"):
        ok, msg = await _run_container()
        if not ok:
            log.warning("mesh container start failed: %s", msg)
            return
        await asyncio.sleep(1)
    await _write_conf(conf)
