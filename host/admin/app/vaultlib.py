"""Account metadata vault: account-wide config sync through the control plane.

Sits ABOVE cluster_sync (D1 in host/docs/linked-accounts.md): intra-cluster
convergence stays peer-to-peer; account-wide convergence is CP-mediated. Each
cluster's cloud coordinator (or a standalone linked node) periodically pulls
the account vault, merges it through cluster_sync.import_state (newer-wins +
tombstones), re-exports, and pushes when the state actually changed —
optimistic concurrency via the vault `version` (pull→merge→retry once on 409).

Secrets (D2): different clusters have different ENCRYPTION_KEYs, so locally
Fernet-encrypted fields can't travel as-is. Export DECRYPTS every known secret
field to plaintext inside the state dict, then encrypts the WHOLE blob with
the 32-byte account data key (ADK) — json → zlib → Fernet(ADK) — so the
control plane only ever stores ADK-ciphertext. Import reverses: decrypt the
blob with the ADK, re-encrypt each secret field under the local cluster key,
then feed the state through cluster_sync.import_state.

Secret fields re-encrypted (everything crypto.encrypt-ed that rides
cluster_sync.export_state):
  - integrations[*].secret_encrypted
  - integrations[*].config.{token_encrypted, connector_token_encrypted,
    client_secret_encrypted}                (Cloudflare state lives in config)
  - settings.webhook.secret_encrypted
  - service_targets[*].state.mesh.wg_private_key_enc  (DB-VM mesh identity)
(ServiceEnvVar values are stored plaintext in the DB and are protected by the
whole-blob ADK encryption, like every other field.)

The ADK is minted by the first node that links, stored locally under the
"account_data_key" setting (crypto.encrypt-ed like other secrets) and escrowed
to the control plane (PUT /v1/accounts/keys/adk, wrapped CP-side by
VAULT_MASTER_KEY). A fresh install fetches it over TLS after linking.

Local sync bookkeeping lives in the "vault_state" setting:
    {version, pushed_hash, pushed_at, pulled_at, error, restoring}
`pushed_hash` is a sha256 of the PRE-encryption state JSON (minus exported_at)
so Fernet nondeterminism never forces a push.
"""

import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import socket
import zlib
from datetime import datetime
from typing import Any, Callable

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import cluster_sync, clusterlib, crypto
from .models import Identity, Integration, Project

log = logging.getLogger("homebox.vault")

ADK_KEY = "account_data_key"        # setting: {"adk_encrypted": ..., "obtained_at": ...}
VAULT_STATE_KEY = "vault_state"
POST_LINK_KEY = "post_link"         # post-link pipeline progress (see post_link_pipeline)
VAULT_FORMAT = 1

# Integration.config keys that hold cluster-key ciphertext (Cloudflare state).
_CONFIG_SECRET_FIELDS = (
    "token_encrypted", "connector_token_encrypted", "client_secret_encrypted",
)


class VaultError(Exception):
    pass


def _now() -> str:
    return datetime.utcnow().isoformat()


# ───── vault_state bookkeeping ────────────────────────────────────────────────


async def get_vault_state(session: AsyncSession) -> dict[str, Any]:
    val = await clusterlib._get_setting(session, VAULT_STATE_KEY)
    return dict(val) if isinstance(val, dict) else {}


async def _save_vault_state(session: AsyncSession, **updates: Any) -> dict[str, Any]:
    vs = await get_vault_state(session)
    vs.update(updates)
    await clusterlib._set_setting(session, VAULT_STATE_KEY, vs)
    await session.commit()
    return vs


async def _account_ctx(session: AsyncSession) -> tuple[str, str] | None:
    """(control_plane_url, account_token_plain) for the linked account."""
    acct = await clusterlib.load_account(session)
    if not acct:
        return None
    return acct["control_plane_url"], crypto.decrypt(acct["token_encrypted"])


# ───── ADK lifecycle ──────────────────────────────────────────────────────────


def _adk_fernet(adk_b64: str) -> Fernet:
    """Fernet keyed by the raw 32-byte ADK. Tolerates standard or urlsafe b64."""
    s = adk_b64.strip()
    try:
        raw = base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    except (ValueError, binascii.Error):
        raise VaultError("account data key is not valid base64")
    if len(raw) != 32:
        raise VaultError("account data key is not 32 bytes")
    return Fernet(base64.urlsafe_b64encode(raw))


async def ensure_adk(session: AsyncSession) -> str:
    """The account data key (urlsafe-b64 of 32 random bytes). Load from the
    local setting; else fetch from the CP escrow; else mint + escrow it (a 409
    on the escrow PUT means another node won the race — adopt its key)."""
    val = await clusterlib._get_setting(session, ADK_KEY)
    if isinstance(val, dict) and val.get("adk_encrypted"):
        adk = crypto.decrypt(val["adk_encrypted"])
        if adk:
            return adk
    ctx = await _account_ctx(session)
    if not ctx:
        raise VaultError("No linked homebox.sh account — cannot establish the account data key.")
    cp_url, token = ctx
    adk_b64 = ""
    try:
        resp = await clusterlib._cp("GET", cp_url, "/v1/accounts/keys/adk", token=token)
        adk_b64 = (resp.get("adk_b64") or "").strip()
    except clusterlib.ControlPlaneError as e:
        if e.status_code != 404:
            raise
    if not adk_b64:
        adk_b64 = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
        try:
            await clusterlib._cp("PUT", cp_url, "/v1/accounts/keys/adk",
                                 token=token, body={"adk_b64": adk_b64})
        except clusterlib.ControlPlaneError as e:
            if e.status_code == 409:
                # Another node escrowed first — fetch and adopt the winner.
                resp = await clusterlib._cp("GET", cp_url, "/v1/accounts/keys/adk", token=token)
                adk_b64 = (resp.get("adk_b64") or "").strip()
            else:
                raise
    _adk_fernet(adk_b64)  # validate shape before persisting
    await clusterlib._set_setting(session, ADK_KEY, {
        "adk_encrypted": crypto.encrypt(adk_b64),
        "obtained_at": _now(),
    })
    await session.commit()
    return adk_b64


# ───── secret-field re-encryption ─────────────────────────────────────────────


def _walk_secret_fields(state: dict[str, Any],
                        transform: Callable[[str], str]) -> dict[str, Any]:
    """Deep copy of an export_state dict with every known secret field passed
    through `transform`. Best-effort per field: a transform returning "" (an
    undecryptable blob) leaves the original value in place, mirroring
    clusterlib._reencrypt_local_secrets."""
    out = json.loads(json.dumps(state, default=str))  # deep, JSON-safe copy

    def tx(container: dict, field: str) -> None:
        val = container.get(field)
        if isinstance(val, str) and val:
            new = transform(val)
            if new:
                container[field] = new

    for integ in out.get("integrations") or []:
        tx(integ, "secret_encrypted")
        cfg = integ.get("config")
        if isinstance(cfg, dict):
            for f in _CONFIG_SECRET_FIELDS:
                tx(cfg, f)
    wh = (out.get("settings") or {}).get("webhook")
    if isinstance(wh, dict):
        tx(wh, "secret_encrypted")
    for st in out.get("service_targets") or []:
        mesh = (st.get("state") or {}).get("mesh")
        if isinstance(mesh, dict):
            tx(mesh, "wg_private_key_enc")
    return out


def state_hash(state: dict[str, Any]) -> str:
    """Stable content hash of a (pre-encryption, plaintext-secrets) state dict.
    exported_at is excluded so an unchanged config never re-pushes."""
    hashable = {k: v for k, v in state.items() if k != "exported_at"}
    return hashlib.sha256(json.dumps(
        hashable, sort_keys=True, separators=(",", ":"), default=str
    ).encode()).hexdigest()


# ───── export / import ────────────────────────────────────────────────────────


async def build_vault_state(session: AsyncSession) -> dict[str, Any]:
    """cluster_sync export with secret fields decrypted to PLAINTEXT — only
    ever wrapped in ADK Fernet before leaving the process."""
    node_id = await clusterlib.get_node_id(session)
    state = await cluster_sync.export_state(session, node_id)
    return _walk_secret_fields(state, lambda v: crypto.decrypt(v) or "")


async def export_vault(session: AsyncSession,
                       adk_b64: str | None = None) -> tuple[str, str]:
    """(blob_b64, inner_state_hash). The blob is Fernet(ADK) over
    zlib(json({"format", "state", "generated_at"})) — a Fernet token is already
    urlsafe-base64, so it ships as-is."""
    adk_b64 = adk_b64 or await ensure_adk(session)
    state = await build_vault_state(session)
    wrapper = {"format": VAULT_FORMAT, "state": state, "generated_at": _now()}
    raw = zlib.compress(json.dumps(wrapper, separators=(",", ":"), default=str).encode())
    blob_b64 = _adk_fernet(adk_b64).encrypt(raw).decode("ascii")
    return blob_b64, state_hash(state)


async def import_vault(session: AsyncSession, blob_b64: str, *, mode: str = "update",
                       adk_b64: str | None = None) -> dict[str, int]:
    """Decrypt/decompress a vault blob, re-encrypt its secret fields under the
    LOCAL cluster key, then merge via cluster_sync.import_state (newer-wins,
    tombstones, service_target timestamp groups — all reused, not re-done)."""
    adk_b64 = adk_b64 or await ensure_adk(session)
    try:
        raw = _adk_fernet(adk_b64).decrypt(blob_b64.encode("ascii"))
    except (InvalidToken, ValueError):
        raise VaultError("vault blob does not decrypt with the account data key")
    try:
        wrapper = json.loads(zlib.decompress(raw))
    except (zlib.error, ValueError):
        raise VaultError("vault blob is corrupt (bad compression/JSON)")
    if not isinstance(wrapper, dict) or wrapper.get("format") != VAULT_FORMAT:
        raise VaultError(f"unsupported vault format {wrapper.get('format') if isinstance(wrapper, dict) else wrapper!r}")
    state = _walk_secret_fields(wrapper.get("state") or {}, crypto.encrypt)
    return await cluster_sync.import_state(session, state, mode=mode)


# ───── steady-state tick (cluster loop) ───────────────────────────────────────


async def should_run_vault(session: AsyncSession) -> bool:
    """Vault sync runs on exactly one node per cluster: the cloud coordinator —
    or on a standalone (not clustered) linked node."""
    if not await clusterlib.load_account(session):
        return False
    state = await clusterlib.load_cluster(session)
    if not state:
        return True
    from . import targetslib
    return await targetslib.is_cloud_coordinator(session, state)


async def _push_vault(session: AsyncSession, cp_url: str, token: str, adk: str,
                      blob_b64: str, digest: str, version_expected: int) -> int:
    """PUT with version CAS; on 409 pull→merge→re-export→retry ONCE."""
    node_id = await clusterlib.get_node_id(session)
    state = await clusterlib.load_cluster(session)
    meta = {"node_id": node_id, "cluster_id": (state or {}).get("cluster_id"),
            "generated_at": _now()}
    try:
        resp = await clusterlib._cp(
            "PUT", cp_url, "/v1/accounts/vault", token=token,
            body={"version_expected": version_expected, "blob_b64": blob_b64,
                  "meta": meta})
    except clusterlib.ControlPlaneError as e:
        if e.status_code != 409:
            raise
        # Lost the CAS race: another writer landed first. Pull + merge, then
        # push the merged state exactly once more.
        remote = await clusterlib._cp("GET", cp_url, "/v1/accounts/vault", token=token)
        rv = int(remote.get("version") or 0)
        if remote.get("blob_b64"):
            await import_vault(session, remote["blob_b64"], mode="update", adk_b64=adk)
            await _save_vault_state(session, version=rv, pulled_at=_now())
        blob_b64, digest = await export_vault(session, adk)
        meta["generated_at"] = _now()
        resp = await clusterlib._cp(
            "PUT", cp_url, "/v1/accounts/vault", token=token,
            body={"version_expected": rv, "blob_b64": blob_b64, "meta": meta})
    new_version = int(resp.get("version") or 0)
    await _save_vault_state(session, version=new_version, pushed_hash=digest,
                            pushed_at=_now(), error=None)
    return new_version


async def vault_tick(session: AsyncSession) -> dict[str, Any] | None:
    """One pull→merge→push cycle. Called from the cluster loop; never raises —
    failures are logged and recorded on the vault_state setting."""
    try:
        if not await should_run_vault(session):
            return None
        ctx = await _account_ctx(session)
        if not ctx:
            return None
        cp_url, token = ctx
        adk = await ensure_adk(session)
        vs = await get_vault_state(session)
        known_version = int(vs.get("version") or 0)

        # Pull: import when the remote vault moved past what we last saw.
        try:
            remote = await clusterlib._cp("GET", cp_url, "/v1/accounts/vault", token=token)
        except clusterlib.ControlPlaneError as e:
            if e.status_code != 404:
                raise
            remote = None
        if remote is not None:
            rv = int(remote.get("version") or 0)
            if rv > known_version and remote.get("blob_b64"):
                await import_vault(session, remote["blob_b64"], mode="update", adk_b64=adk)
                vs = await _save_vault_state(session, version=rv, pulled_at=_now(),
                                             error=None)
            known_version = max(known_version, rv)

        # Push: only when the (pre-encryption) state actually changed.
        blob_b64, digest = await export_vault(session, adk)
        if digest != vs.get("pushed_hash"):
            version = await _push_vault(session, cp_url, token, adk,
                                        blob_b64, digest, known_version)
            return {"pushed": True, "version": version}
        if vs.get("error"):
            await _save_vault_state(session, error=None)
        return {"pushed": False, "version": known_version}
    except (clusterlib.ControlPlaneError, VaultError) as e:
        log.warning("vault tick failed: %s", e)
        try:
            await _save_vault_state(session, error=str(e))
        except Exception:  # noqa: BLE001
            log.exception("failed recording vault error")
        return None


# ───── restore on account link ────────────────────────────────────────────────


async def _local_db_is_fresh(session: AsyncSession) -> bool:
    """A DB with no projects and no integrations is a fresh install → the vault
    import may use full-restore (overwrite) semantics."""
    has_project = (await session.execute(select(Project.id).limit(1))).first()
    has_integ = (await session.execute(select(Integration.id).limit(1))).first()
    return not has_project and not has_integ


async def restore_on_link(session: AsyncSession) -> dict[str, Any]:
    """Right after a successful account link: fetch the ADK + vault and import
    (mode "full" on a fresh DB, "update" otherwise), then push our merged state.
    Progress/errors surface via the vault_state setting."""
    ctx = await _account_ctx(session)
    if not ctx:
        raise VaultError("No linked homebox.sh account.")
    cp_url, token = ctx
    await _save_vault_state(session, restoring=True, error=None)
    try:
        adk = await ensure_adk(session)
        try:
            remote = await clusterlib._cp("GET", cp_url, "/v1/accounts/vault", token=token)
        except clusterlib.ControlPlaneError as e:
            if e.status_code != 404:
                raise
            remote = None
        imported: dict[str, int] | None = None
        if remote is not None and remote.get("blob_b64"):
            mode = "full" if await _local_db_is_fresh(session) else "update"
            imported = await import_vault(session, remote["blob_b64"], mode=mode,
                                          adk_b64=adk)
            await _save_vault_state(session, version=int(remote.get("version") or 0),
                                    pulled_at=_now())
        vs = await get_vault_state(session)
        blob_b64, digest = await export_vault(session, adk)
        if digest != vs.get("pushed_hash"):
            await _push_vault(session, cp_url, token, adk, blob_b64, digest,
                              int(vs.get("version") or 0))
        await _save_vault_state(session, restoring=False, error=None)
        return {"imported": imported}
    except Exception as e:
        try:
            await _save_vault_state(session, restoring=False, error=str(e))
        except Exception:  # noqa: BLE001
            pass
        raise


# ───── post-link pipeline (restore → identity → default cluster) ─────────────
#
# One pipeline runs after EVERY successful account link (OAuth popup, silent
# re-auth, token paste):
#   1. vault restore + first push (restore_on_link — progress on vault_state)
#   2. G3b: upsert an enabled Identity for the account's verified email, so
#      provider login works on this box from now on (whitelist stays intact
#      before/without a link — this only ever runs post-link)
#   3. G6: when the node is NOT in a cluster, found a NEW empty cluster named
#      after the machine (deduped against the account's clusters). CP 402
#      (free plan) → stay standalone silently. NEVER joins an existing
#      cluster — joining stays an explicit god-view/manual action.
#
# Progress rides the "post_link" setting so the UI can show
# link → sync → cluster-created states:
#   {stage: restoring|identity|cluster|done, started_at, updated_at,
#    finished_at?, restore_ok, restore_error, identity_email, identity_created,
#    identity_error, cluster_created, cluster_id, cluster_name, standalone,
#    cluster_error, error}
#
# Sequencing/restart note: create_cluster_flow does NOT restart the admin (the
# seed keeps its own keys — only join_cluster_flow calls restart_self_soon).
# The restore and identity steps still commit BEFORE the cluster step runs, so
# even if a restart were ever introduced there, everything earlier persists.


async def get_post_link_state(session: AsyncSession) -> dict[str, Any]:
    val = await clusterlib._get_setting(session, POST_LINK_KEY)
    return dict(val) if isinstance(val, dict) else {}


async def _save_post_link(session: AsyncSession, **updates: Any) -> dict[str, Any]:
    st = await get_post_link_state(session)
    st.update(updates)
    st["updated_at"] = _now()
    await clusterlib._set_setting(session, POST_LINK_KEY, st)
    await session.commit()
    return st


async def _ensure_identity_for_account(
    session: AsyncSession, *, email: str | None = None,
) -> dict[str, Any]:
    """G3b: upsert an ENABLED Identity for the linked account's verified email
    (the control-plane account is the trust anchor). The email comes from the
    caller (CP register response) or GET /v1/accounts/me. Commits."""
    email = (email or "").strip().lower()
    if not email:
        ctx = await _account_ctx(session)
        if not ctx:
            return {"error": "no linked account"}
        cp_url, token = ctx
        me = await clusterlib._cp("GET", cp_url, "/v1/accounts/me", token=token)
        email = ((me.get("email") or "") if isinstance(me, dict) else "").strip().lower()
    if not email:
        return {"error": "the account has no verified email"}
    row = (
        await session.execute(select(Identity).where(Identity.email == email))
    ).scalar_one_or_none()
    created = False
    if row is None:
        session.add(Identity(email=email, enabled=True))
        created = True
    elif not row.enabled:
        row.enabled = True
    await session.commit()
    return {"email": email, "created": created}


async def _default_cluster_name(session: AsyncSession, cp_url: str, token: str) -> str:
    """Machine-derived cluster name, deduped against the account's clusters."""
    acct = await clusterlib.load_account(session)
    base = ((acct or {}).get("node_name") or "").strip() \
        or (socket.gethostname() or "").strip() or "homebox"
    existing: set[str] = set()
    try:
        topo = await clusterlib._cp("GET", cp_url, "/v1/accounts/topology", token=token)
        existing = {
            str(c.get("name") or "").strip().lower()
            for c in (topo.get("clusters") or []) if isinstance(c, dict)
        }
    except clusterlib.ControlPlaneError:
        pass  # can't list — the CP's own name handling is the backstop
    name, i = base, 2
    while name.lower() in existing:
        name = f"{base}-{i}"
        i += 1
    return name


async def _maybe_create_default_cluster(session: AsyncSession) -> dict[str, Any]:
    """G6: found a NEW empty cluster with this node as the seed — never join.
    CP 402 (plan without the cluster feature) → standalone, silently."""
    if await clusterlib.load_cluster(session):
        return {"created": False, "reason": "already in a cluster"}
    ctx = await _account_ctx(session)
    if not ctx:
        return {"created": False, "reason": "no linked account"}
    cp_url, token = ctx
    acct = await clusterlib.load_account(session)
    name = await _default_cluster_name(session, cp_url, token)
    try:
        state = await clusterlib.create_cluster_flow(
            session,
            control_plane_url=cp_url,
            account_token_plain=token,
            name=name,
            peer_url=(acct or {}).get("peer_url") or "",
            node_name=(acct or {}).get("node_name") or "",
        )
    except clusterlib.ControlPlaneError as e:
        if e.status_code == 402:
            return {"created": False, "standalone": True,
                    "reason": "plan has no cluster feature"}
        raise
    return {"created": True, "cluster_id": state["cluster_id"],
            "name": state["name"]}


async def post_link_pipeline(
    session: AsyncSession, *, provider: str | None = None, email: str | None = None,
) -> dict[str, Any]:
    """Run the whole post-link pipeline (see the section comment above). Never
    raises — each stage is best-effort and recorded on the post_link setting."""
    await _save_post_link(
        session, stage="restoring", started_at=_now(), finished_at=None,
        error=None, restore_ok=None, restore_error=None,
        identity_email=None, identity_created=None, identity_error=None,
        cluster_created=None, cluster_id=None, cluster_name=None,
        standalone=None, cluster_error=None, provider=provider,
    )

    # 1. vault restore + first push (detail also rides vault_state)
    restore_ok, restore_error = True, None
    try:
        await restore_on_link(session)
    except Exception as e:  # noqa: BLE001 — the link itself already succeeded
        log.exception("post-link: vault restore failed")
        restore_ok, restore_error = False, str(e)
    await _save_post_link(session, stage="identity",
                          restore_ok=restore_ok, restore_error=restore_error)

    # 2. G3b: identity auto-create from the linked account
    try:
        ident = await _ensure_identity_for_account(session, email=email)
    except Exception as e:  # noqa: BLE001
        log.exception("post-link: identity auto-create failed")
        ident = {"error": str(e)}
    await _save_post_link(
        session, stage="cluster",
        identity_email=ident.get("email"),
        identity_created=bool(ident.get("created")),
        identity_error=ident.get("error"),
    )

    # 3. G6: default new empty cluster — only after a completed restore, and
    #    never a join.
    if restore_ok:
        try:
            cluster = await _maybe_create_default_cluster(session)
        except Exception as e:  # noqa: BLE001
            log.exception("post-link: default cluster creation failed")
            cluster = {"created": False, "error": str(e)}
    else:
        cluster = {"created": False, "reason": "restore did not complete"}
    final = await _save_post_link(
        session, stage="done", finished_at=_now(),
        cluster_created=bool(cluster.get("created")),
        cluster_id=cluster.get("cluster_id"),
        cluster_name=cluster.get("name"),
        standalone=bool(cluster.get("standalone")),
        cluster_error=cluster.get("error"),
        error=restore_error or ident.get("error") or cluster.get("error"),
    )
    return final


def schedule_post_link(*, provider: str | None = None, email: str | None = None) -> None:
    """Fire the post-link pipeline in the background with its own session so
    the account-link HTTP response isn't blocked. Progress rides the
    post_link + vault_state settings."""
    async def _run() -> None:
        from .db import SessionLocal
        async with SessionLocal() as session:
            try:
                await post_link_pipeline(session, provider=provider, email=email)
            except Exception:  # noqa: BLE001 — the link itself already succeeded
                log.exception("post-link pipeline failed")
    try:
        asyncio.get_event_loop().create_task(_run())
    except RuntimeError:
        log.warning("no running event loop — post-link pipeline will run on the next cluster loop tick")


def schedule_restore_on_link() -> None:
    """Back-compat alias: every link path now runs the full post-link pipeline
    (restore → identity auto-create → default cluster)."""
    schedule_post_link()
