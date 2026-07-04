"""Cloudflare REST API client + DB-backed credentials store.

Architecture: we run cloudflared in *remotely-managed* mode. A scoped Cloudflare
API token (entered through the UI) is encrypted at rest in the `settings`
table under key='cloudflare'. From it we create/delete tunnels, fetch the
single connector token cloudflared needs, push ingress config, and manage DNS
records — no local cert.pem / config.yml / credentials JSON required.

Token scopes the UI suggests:
  Account · Cloudflare Tunnel · Edit
  Account · Account Settings · Read   (lists accounts)
  Zone    · DNS · Edit
  Zone    · Zone · Read
"""

import base64
import json
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .crypto import decrypt, encrypt
from .models import Integration

API = "https://api.cloudflare.com/client/v4"
PROVIDER = "cloudflare"


# ───── DB-backed credentials/state ────────────────────────────────────────────
#
# Cloudflare creds + tunnel state live as the single Integration(provider=
# 'cloudflare') row. The `state` dict (token_encrypted, account_id, tunnel_id,
# connector_token_encrypted…) is persisted in Integration.config, and a few
# non-secret bits are mirrored to columns for the Integrations UI. The dict API
# below is unchanged, so tunnel.py / onboarding.py / monitor.py are unaffected.


async def _row(session: AsyncSession) -> Integration | None:
    return (
        await session.execute(select(Integration).where(Integration.provider == PROVIDER))
    ).scalar_one_or_none()


async def load_state(session: AsyncSession) -> dict[str, Any]:
    """Returns the current Cloudflare state (token / account / tunnel) or {}."""
    row = await _row(session)
    return dict(row.config) if row and row.config else {}


async def save_state(session: AsyncSession, state: dict[str, Any]) -> None:
    row = await _row(session)
    if row is None:
        row = Integration(provider=PROVIDER)
        session.add(row)
    row.config = state
    # Mirror non-secret bits to columns so the Integrations page can list it.
    row.account_id = state.get("account_id")
    row.account_login = state.get("account_name")
    row.name = state.get("account_name") or "Cloudflare"
    row.secret_encrypted = state.get("token_encrypted")
    row.status = "connected" if state.get("token_encrypted") else "disconnected"
    row.updated_at = datetime.utcnow()
    await session.commit()


async def clear_state(session: AsyncSession) -> None:
    row = await _row(session)
    if row is not None:
        await session.delete(row)
        await session.commit()


def get_token(state: dict[str, Any]) -> str | None:
    enc = state.get("token_encrypted")
    if not enc:
        return None
    return decrypt(enc) or None


def get_connector_token(state: dict[str, Any]) -> str | None:
    enc = state.get("connector_token_encrypted")
    if not enc:
        return None
    return decrypt(enc) or None


def store_token(state: dict[str, Any], token: str) -> None:
    state["token_encrypted"] = encrypt(token)


def store_connector_token(state: dict[str, Any], connector_token: str) -> None:
    state["connector_token_encrypted"] = encrypt(connector_token)


# ───── HTTP helpers ───────────────────────────────────────────────────────────


class CloudflareError(Exception):
    """Raised when the Cloudflare API returns an error envelope."""

    def __init__(self, status: int, message: str, errors: list[Any] | None = None):
        super().__init__(message)
        self.status = status
        self.errors = errors or []


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "homebox-admin",
    }


def _unwrap(r: httpx.Response) -> Any:
    try:
        body = r.json()
    except ValueError:
        raise CloudflareError(r.status_code, f"Non-JSON response ({r.status_code})")
    if not isinstance(body, dict) or not body.get("success"):
        errs = (body or {}).get("errors") or []
        if errs:
            first = errs[0]
            code = first.get("code")
            msg = first.get("message") or f"Cloudflare API {r.status_code}"
            # Include the numeric error code — CF's "Authentication error" is
            # the same string for half a dozen different underlying causes,
            # but the code disambiguates (10000 = invalid creds, 9109 = scope
            # mismatch, 9103 = wrong endpoint level, etc.).
            if code:
                msg = f"{msg} (code {code})"
        else:
            msg = f"Cloudflare API {r.status_code}"
        raise CloudflareError(r.status_code, msg, errs)
    return body.get("result")


# ───── API calls ──────────────────────────────────────────────────────────────


async def verify_token(token: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{API}/user/tokens/verify", headers=_headers(token))
    return _unwrap(r) or {}


async def list_accounts(token: str) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(f"{API}/accounts", headers=_headers(token), params={"per_page": 50})
    return _unwrap(r) or []


async def list_zones(token: str, account_id: str | None = None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"per_page": 50}
    if account_id:
        params["account.id"] = account_id
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API}/zones", headers=_headers(token), params=params)
    return _unwrap(r) or []


async def list_tunnels(
    token: str, account_id: str, *, name: str | None = None,
) -> list[dict[str, Any]]:
    """List non-deleted tunnels in the account, optionally filtered by exact name.
    Used to detect a name collision and adopt the existing tunnel instead of
    failing the create call."""
    params: dict[str, Any] = {"per_page": 50, "is_deleted": "false"}
    if name:
        params["name"] = name
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"{API}/accounts/{account_id}/cfd_tunnel",
            headers=_headers(token),
            params=params,
        )
    return _unwrap(r) or []


async def create_tunnel(
    token: str, account_id: str, name: str, *,
    config_src: str = "cloudflare",
    metadata: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a remotely-managed tunnel. `config_src=cloudflare` means ingress
    config is stored on Cloudflare's side (PUT /configurations), not in a
    local config.yml — exactly what we want for UI-driven management.

    `metadata` is an arbitrary string-keyed map persisted on the tunnel.
    Homebox tags every tunnel it creates so we can recognize our own when
    the user re-runs onboarding (rather than auto-adopting a same-named
    tunnel that belongs to something else)."""
    payload: dict[str, Any] = {"name": name, "config_src": config_src}
    if metadata:
        payload["metadata"] = metadata
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"{API}/accounts/{account_id}/cfd_tunnel",
            headers=_headers(token),
            json=payload,
        )
    return _unwrap(r) or {}


async def delete_tunnel(token: str, account_id: str, tunnel_id: str) -> None:
    async with httpx.AsyncClient(timeout=15) as c:
        # `cascade=true` cleans up DNS routes and connector registrations.
        r = await c.delete(
            f"{API}/accounts/{account_id}/cfd_tunnel/{tunnel_id}",
            headers=_headers(token),
            params={"cascade": "true"},
        )
    if r.status_code == 404:
        return  # Already gone — fine.
    _unwrap(r)


async def get_tunnel(token: str, account_id: str, tunnel_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(
            f"{API}/accounts/{account_id}/cfd_tunnel/{tunnel_id}",
            headers=_headers(token),
        )
    return _unwrap(r) or {}


async def get_connector_token_for(token: str, account_id: str, tunnel_id: str) -> str:
    """Returns the long-lived connector token cloudflared runs with."""
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get(
            f"{API}/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token",
            headers=_headers(token),
        )
    result = _unwrap(r)
    if not isinstance(result, str):
        raise CloudflareError(r.status_code, "Cloudflare did not return a connector token")
    return result


async def put_tunnel_config(
    token: str, account_id: str, tunnel_id: str, ingress: list[dict[str, Any]]
) -> dict[str, Any]:
    """Replace the tunnel's ingress rules. cloudflared picks up the change
    without a restart. The last entry MUST be a catch-all (`service`-only)."""
    payload = {"config": {"ingress": ingress}}
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.put(
            f"{API}/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
            headers=_headers(token),
            json=payload,
        )
    return _unwrap(r) or {}


async def create_zone(token: str, account_id: str, name: str) -> dict[str, Any]:
    """Create a zone (requires Zone:Edit). Returns the zone dict incl. the
    `name_servers` Cloudflare assigns — the user sets those at their registrar."""
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(
            f"{API}/zones", headers=_headers(token),
            json={"name": name, "account": {"id": account_id}, "type": "full"},
        )
        return _unwrap(r)


async def get_zone(token: str, zone_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(f"{API}/zones/{zone_id}", headers=_headers(token))
        return _unwrap(r)


async def list_dns_records(
    token: str, zone_id: str, *, name: str | None = None
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"per_page": 100}
    if name:
        params["name"] = name
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"{API}/zones/{zone_id}/dns_records",
            headers=_headers(token),
            params=params,
        )
    return _unwrap(r) or []


async def upsert_cname(
    token: str,
    zone_id: str,
    name: str,
    target: str,
    *,
    proxied: bool = True,
) -> dict[str, Any]:
    """Create-or-update a CNAME record. If a record at `name` already points
    elsewhere, this overwrites it (matching `cloudflared --overwrite-dns`)."""
    existing = await list_dns_records(token, zone_id, name=name)
    payload = {
        "type": "CNAME",
        "name": name,
        "content": target,
        "proxied": proxied,
        "ttl": 1,
        "comment": "Managed by Homebox",
    }
    async with httpx.AsyncClient(timeout=15) as c:
        if existing:
            rec_id = existing[0]["id"]
            r = await c.put(
                f"{API}/zones/{zone_id}/dns_records/{rec_id}",
                headers=_headers(token),
                json=payload,
            )
        else:
            r = await c.post(
                f"{API}/zones/{zone_id}/dns_records",
                headers=_headers(token),
                json=payload,
            )
    return _unwrap(r) or {}


def tunnel_target(tunnel_id: str) -> str:
    return f"{tunnel_id}.cfargotunnel.com"


def resolve_zone_for(
    zones: list[dict[str, Any]], hostname: str
) -> dict[str, Any] | None:
    """Pick the Cloudflare zone that owns `hostname`: the longest zone name that
    equals the hostname or is a parent suffix of it. Lets us repoint DNS for a
    stored Domain (e.g. `homebox.x100.dev`) without persisting its zone id —
    the zone (`x100.dev`) is recovered from the account's zone list. Returns the
    zone dict or None when no connected zone covers the hostname."""
    host = hostname.strip().lower().strip(".")
    best: dict[str, Any] | None = None
    best_len = -1
    for z in zones:
        zname = (z.get("name") or "").strip().lower().strip(".")
        if not zname:
            continue
        if (host == zname or host.endswith("." + zname)) and len(zname) > best_len:
            best, best_len = z, len(zname)
    return best


def build_ingress(domains: list[dict[str, Any]], service_url: str = "http://traefik:80") -> list[dict[str, Any]]:
    """Build the Cloudflare-side ingress array from Domain rows."""
    rules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in domains:
        for hostname in (d["name"], f"*.{d['name']}"):
            if hostname in seen:
                continue
            seen.add(hostname)
            rules.append({"hostname": hostname, "service": service_url})
    rules.append({"service": "http_status:404"})
    return rules
