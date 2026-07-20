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
  Account · Cloudflare Pages · Edit   (only for the Pages deployment target;
                                       older tokens keep working — Pages
                                       deploys fail with a re-scope hint)
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


async def delete_dns_record(token: str, zone_id: str, record_id: str) -> None:
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.delete(
            f"{API}/zones/{zone_id}/dns_records/{record_id}", headers=_headers(token)
        )
        if r.status_code != 404:
            _unwrap(r)


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


async def upsert_txt(
    token: str,
    zone_id: str,
    name: str,
    content: str,
) -> dict[str, Any]:
    """Create-or-update a TXT record (cloud-target verification records, e.g.
    Google site verification for Cloud Run domain mappings). Matched by
    (name, exact content): verification flows may legitimately coexist with
    other TXT records at the same name, so an existing different-content
    record is left alone and a new one is added."""
    existing = await list_dns_records(token, zone_id, name=name)
    payload = {
        "type": "TXT",
        "name": name,
        "content": content,
        "ttl": 1,
        "comment": "Managed by Homebox",
    }
    # Cloudflare returns TXT content quoted; compare unquoted.
    match = next((r for r in existing if r.get("type") == "TXT"
                  and (r.get("content") or "").strip('"') == content.strip('"')), None)
    if match:
        return match
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"{API}/zones/{zone_id}/dns_records",
            headers=_headers(token),
            json=payload,
        )
    return _unwrap(r) or {}


def tunnel_target(tunnel_id: str) -> str:
    return f"{tunnel_id}.cfargotunnel.com"


# ── cross-cluster domain ownership (per-host DNS overrides, G12) ──────────────
#
# Domains + this integration sync account-wide, but a domain's apex/wildcard
# CNAME points at the tunnel of whichever cluster CONNECTED it (routes/
# domains.py). These helpers answer "does THIS install's tunnel own that
# wildcard routing?" so a deploy on another cluster knows it must write a
# specific-host record instead of relying on the (foreign) wildcard.


async def wildcard_tunnel_cname(token: str, zone_id: str, zone_name: str) -> str | None:
    """The tunnel target (<id>.cfargotunnel.com, lowercased) of the zone's
    `*.<zone>` CNAME — apex fallback when no wildcard record exists. None when
    neither record exists or the examined record isn't a tunnel CNAME (an A
    record or external CNAME is not a Homebox wildcard and is never treated
    as foreign ownership)."""
    for name in (f"*.{zone_name}", zone_name):
        recs = await list_dns_records(token, zone_id, name=name)
        cname = next((r for r in recs if r.get("type") == "CNAME"), None)
        if cname:
            content = (cname.get("content") or "").strip().lower().strip(".")
            return content if content.endswith(".cfargotunnel.com") else None
    return None


async def domain_owned_by_local_tunnel(
    session: AsyncSession, domain_name: str, *,
    state: dict[str, Any] | None = None,
    cache: dict[str, bool] | None = None,
    strict: bool = False,
) -> bool:
    """Whether THIS install's tunnel owns `domain_name`'s wildcard routing.

    True (owned — caller changes nothing, the wildcard flow stands) when the
    wildcard/apex CNAME points at our tunnel, when neither record exists or
    isn't a tunnel CNAME, when no zone in the account covers the domain, when
    no tunnel is configured here, or — fail-safe — on any Cloudflare API error
    (strict=True re-raises instead, for callers that must not act on a guess).
    False only when the record demonstrably points at a DIFFERENT tunnel.
    `cache` (domain -> bool) dedupes probes within one deploy run."""
    key = domain_name.strip().lower().strip(".")
    if cache is not None and key in cache:
        return cache[key]
    owned = True
    try:
        st = state if state is not None else await load_state(session)
        token = get_token(st)
        tunnel_id = st.get("tunnel_id")
        if token and tunnel_id:
            zones = await list_zones(token, account_id=st.get("account_id"))
            zone = resolve_zone_for(zones, key)
            if zone:
                actual = await wildcard_tunnel_cname(token, zone["id"], zone["name"])
                if actual is not None:
                    owned = actual == tunnel_target(tunnel_id).lower()
    except CloudflareError:
        if strict:
            raise
        owned = True  # fail-safe: behave as before (no override records)
    if cache is not None:
        cache[key] = owned
    return owned


async def list_pages_projects(token: str, account_id: str) -> list[dict[str, Any]]:
    """Pages projects in the account — doubles as the 'does this token carry
    Cloudflare Pages: Edit' capability probe (403 without it)."""
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(
            f"{API}/accounts/{account_id}/pages/projects", headers=_headers(token)
        )
    return _unwrap(r) or []


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


def build_ingress(
    domains: list[dict[str, Any]],
    service_url: str = "http://traefik:80",
    tcp_rules: list[dict[str, Any]] | None = None,
    extra_hostnames: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Build the Cloudflare-side ingress array from Domain rows.

    `tcp_rules` are `{"hostname": ..., "service": "tcp://<container>:<port>"}`
    entries for database/cache hosts serverless workloads dial through the
    tunnel (see targetslib.all_tunnel_tcp_rules). They are PREPENDED: ingress
    rules match top-down, so the specific TCP hostnames must precede the
    domain wildcard rules — and the catch-all stays last.

    `extra_hostnames` are specific hosts THIS tunnel serves under a domain
    whose wildcard belongs to ANOTHER cluster's tunnel (per-host DNS overrides
    — targetslib.load_dns_overrides). Each gets an explicit http rule to
    `service_url`, placed before the domain rules like the tcp entries."""
    rules: list[dict[str, Any]] = []
    seen: set[str] = set()
    for t in tcp_rules or []:
        hostname = t["hostname"]
        if hostname in seen:
            continue
        seen.add(hostname)
        rules.append({"hostname": hostname, "service": t["service"]})
    for hostname in extra_hostnames or []:
        if hostname in seen:
            continue
        seen.add(hostname)
        rules.append({"hostname": hostname, "service": service_url})
    for d in domains:
        for hostname in (d["name"], f"*.{d['name']}"):
            if hostname in seen:
                continue
            seen.add(hostname)
            rules.append({"hostname": hostname, "service": service_url})
    rules.append({"service": "http_status:404"})
    return rules


# ───── Cloudflare Access (serverless → homebox DB path) ───────────────────────
#
# Serverless consumers (Cloud Run / App Runner) reach homebox-hosted databases
# through the tunnel's TCP ingress. That path is public at the edge, so every
# DB hostname is fronted by an Access application that only admits the
# cluster's shared service token; the wrapper image (targets/artifacts.py
# wrap_with_access_proxy) presents it via `cloudflared access tcp`.

ACCESS_SERVICE_TOKEN_NAME = "homebox-db-access"


async def ensure_access_service_token(
    session: AsyncSession, state: dict[str, Any]
) -> tuple[str, str]:
    """Return (client_id, client_secret) of the cluster's shared Access service
    token, creating it once. The secret is only returned by Cloudflare at
    creation time, so it is encrypted into `state["db_access_token"]` and
    reused from there on every later call."""
    cached = state.get("db_access_token") or {}
    if cached.get("client_id") and cached.get("client_secret_encrypted"):
        secret = decrypt(cached["client_secret_encrypted"])
        if secret:
            return cached["client_id"], secret

    token = get_token(state)
    account_id = state.get("account_id")
    if not token or not account_id:
        raise CloudflareError(0, "Cloudflare token/account not configured")
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(
            f"{API}/accounts/{account_id}/access/service_tokens",
            headers=_headers(token),
            json={"name": ACCESS_SERVICE_TOKEN_NAME, "duration": "8760h"},
        )
    result = _unwrap(r) or {}
    client_id = result.get("client_id")
    client_secret = result.get("client_secret")
    if not client_id or not client_secret:
        raise CloudflareError(
            r.status_code, "Access service token create returned no credentials"
        )
    state["db_access_token"] = {
        "token_id": result.get("id"),
        "client_id": client_id,
        "client_secret_encrypted": encrypt(client_secret),
    }
    await save_state(session, state)
    return client_id, client_secret


async def ensure_access_tcp_app(
    token: str, account_id: str, hostname: str, service_token_id: str
) -> str:
    """Idempotently ensure an Access application fronts `hostname` with a
    non-identity policy admitting the shared service token. Returns the app id.
    Safe to double-run (coordinator handover): matched by domain, the policy
    by its name."""
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(
            f"{API}/accounts/{account_id}/access/apps",
            headers=_headers(token),
            params={"per_page": 100},
        )
        apps = _unwrap(r) or []
        app = next(
            (a for a in apps if (a.get("domain") or "").lower() == hostname.lower()),
            None,
        )
        if app is None:
            r = await c.post(
                f"{API}/accounts/{account_id}/access/apps",
                headers=_headers(token),
                json={
                    "name": f"Homebox DB {hostname}",
                    "domain": hostname,
                    "type": "self_hosted",
                    "session_duration": "24h",
                },
            )
            app = _unwrap(r) or {}
        app_id = app.get("id")
        if not app_id:
            raise CloudflareError(r.status_code, f"Access app for {hostname} has no id")

        r = await c.get(
            f"{API}/accounts/{account_id}/access/apps/{app_id}/policies",
            headers=_headers(token),
        )
        policies = _unwrap(r) or []
        if not any(p.get("name") == "homebox-db-token" for p in policies):
            r = await c.post(
                f"{API}/accounts/{account_id}/access/apps/{app_id}/policies",
                headers=_headers(token),
                json={
                    "name": "homebox-db-token",
                    "decision": "non_identity",
                    "include": [{"service_token": {"token_id": service_token_id}}],
                },
            )
            _unwrap(r)
    return app_id
