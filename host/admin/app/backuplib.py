"""Cloud metadata backup: a sanitized, secret-free snapshot of this node's
config, pushed to the control plane so a linked homebox.sh account can show
(and later help restore) what the node was running.

Strictly metadata — no tokens, no integration credentials, no env-var VALUES
(key names only). Contrast with cluster_sync.export_state, which ships secrets
node-to-node and must never leave the cluster.

Push state lives in the settings table under `cloud_backup`:
    {"hash": <sha256 of the snapshot minus generated_at>,
     "pushed_at": <iso | null>, "error": <str | null>}
Pushes are deduped by hash: cluster_loop calls push_backup_if_changed on its
reconcile cadence and only PUTs when the snapshot actually changed (or the
last attempt errored, so transient failures retry).
"""

import hashlib
import json
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import clusterlib, crypto
from .models import Domain, Environment, Identity, Project, Service, ServiceEnvVar

log = logging.getLogger("homebox.backup")

CLOUD_BACKUP_KEY = "cloud_backup"


async def build_snapshot(session: AsyncSession) -> dict[str, Any]:
    """Sanitized, secret-free snapshot of this node's config. Lists are sorted
    by natural key so the serialized form (and thus the change hash) is stable
    across row-order differences."""
    state = await clusterlib.load_cluster(session)
    acct = await clusterlib.load_account(session)
    ident = state or acct or {}  # cluster blob first; account blob when standalone
    admin_domain = await clusterlib._get_setting(session, clusterlib.ADMIN_DOMAIN_KEY)

    domains = (await session.execute(select(Domain))).scalars().all()
    projects = (await session.execute(select(Project))).scalars().all()
    environments = (await session.execute(select(Environment))).scalars().all()
    services = (await session.execute(select(Service))).scalars().all()
    env_vars = (await session.execute(select(ServiceEnvVar))).scalars().all()
    identities = (await session.execute(select(Identity))).scalars().all()

    domain_by_id = {d.id: d for d in domains}
    envs_by_project: dict[int, list[Environment]] = {}
    for e in environments:
        envs_by_project.setdefault(e.project_id, []).append(e)
    svcs_by_project: dict[int, list[Service]] = {}
    for s in services:
        svcs_by_project.setdefault(s.project_id, []).append(s)
    # Key NAMES only — values can hold secrets and must never leave the node.
    keys_by_service: dict[int, set[str]] = {}
    for v in env_vars:
        keys_by_service.setdefault(v.service_id, set()).add(v.key)

    return {
        "generated_at": datetime.utcnow().isoformat(),
        "node": {
            "node_name": ident.get("node_name") or "",
            "peer_url": ident.get("peer_url") or "",
            "admin_domain": admin_domain if isinstance(admin_domain, str) else None,
            "version": clusterlib.VERSION,
        },
        "domains": [
            {"name": d.name, "is_primary": d.is_primary,
             "cloudflare_routed": d.cloudflare_routed, "zone_status": d.zone_status}
            for d in sorted(domains, key=lambda d: d.name)
        ],
        "projects": [
            {
                "name": p.name,
                "repo_full_name": p.repo_full_name,
                "default_branch": p.default_branch,
                "description": p.description,
                "domain": domain_by_id[p.domain_id].name if p.domain_id in domain_by_id else None,
                "environments": [
                    {"name": e.name, "kind": e.kind, "branch": e.branch}
                    for e in sorted(envs_by_project.get(p.id, []), key=lambda e: e.name)
                ],
                "services": [
                    {"name": s.name, "kind": s.kind, "is_public": s.is_public,
                     "subdomain_label": s.subdomain_label,
                     "internal_port": s.internal_port,
                     "env_keys": sorted(keys_by_service.get(s.id, set()))}
                    for s in sorted(svcs_by_project.get(p.id, []), key=lambda s: s.name)
                ],
            }
            for p in sorted(projects, key=lambda p: p.name)
        ],
        "identities": sorted(i.email for i in identities),
    }


async def push_backup_if_changed(session: AsyncSession) -> None:
    """Push the metadata snapshot to the control plane when it changed since
    the last successful push. No-op unless a homebox.sh account is linked.
    Never raises — a failure is recorded on the `cloud_backup` setting (so the
    UI can surface it) and retried on the next cycle."""
    acct = await clusterlib.load_account(session)
    if not acct:
        return
    snapshot = await build_snapshot(session)
    hashable = {k: v for k, v in snapshot.items() if k != "generated_at"}
    digest = hashlib.sha256(
        json.dumps(hashable, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()
    prev = await clusterlib._get_setting(session, CLOUD_BACKUP_KEY)
    prev = prev if isinstance(prev, dict) else {}
    if prev.get("hash") == digest and (not prev.get("error") or prev.get("oversized")):
        # Unchanged since the last push — or unchanged since a 413 (oversized),
        # which retrying identically can never fix.
        return
    node_id = await clusterlib.get_node_id(session)
    try:
        await clusterlib._cp(
            "PUT", acct["control_plane_url"], f"/v1/accounts/nodes/{node_id}/backup",
            token=crypto.decrypt(acct["token_encrypted"]),
            body={"payload": snapshot},
        )
    except clusterlib.ControlPlaneError as e:
        log.warning("cloud backup push failed: %s", e)
        # A 413 (snapshot over the CP's size cap) can't succeed on retry —
        # record the digest so we only try again when the snapshot changes.
        oversized = e.status_code == 413
        await clusterlib._set_setting(session, CLOUD_BACKUP_KEY, {
            "hash": digest if oversized else prev.get("hash"),
            "pushed_at": prev.get("pushed_at"),
            "error": str(e),
            "oversized": oversized,
        })
        await session.commit()
        return
    await clusterlib._set_setting(session, CLOUD_BACKUP_KEY, {
        "hash": digest,
        "pushed_at": datetime.utcnow().isoformat(),
        "error": None,
    })
    await session.commit()
    log.info("cloud backup pushed (%s…)", digest[:12])
