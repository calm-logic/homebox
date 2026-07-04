"""Cluster config sync: export/import of cluster-scoped state between nodes.

Rows travel by NATURAL key (repo name, domain name, provider+account, …) —
never by integer PK, since each node's admin DB assigns its own ids. Import
remaps foreign keys through lookups.

What syncs (cluster-scoped): integrations (secrets included — every node
shares the cluster ENCRYPTION_KEY so blobs decrypt anywhere), domains,
projects, environments, services, service env vars, identities, and the
webhook/admin_domain settings. What stays node-local: install_id, node_keys,
cluster membership, metric/uptime samples, dns_status, and full deployment
history (only a latest-per-env summary is exported, for reconcile).

Modes:
  full    initial sync after join — upsert AND overwrite fields
  deploy  pulled by a peer when a deploy is fanned out to it — like full, so
          env-var/domain edits ride along with the deploy that uses them
  update  periodic reconcile — additive only (adds missing rows, updates
          nothing except integrations, which carry updated_at for a real
          newer-wins comparison). Avoids two nodes ping-ponging stale copies
          of rows that have no timestamp to compare.

Deletions do NOT propagate yet (documented limitation — needs tombstones or
the Spock-based replication from the design doc).
"""

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import (
    Deployment, Domain, Environment, Identity, Integration, Project, Service,
    ServiceEnvVar, Setting,
)

log = logging.getLogger("homebox.cluster.sync")

SCHEMA = 1
# Settings keys that are cluster-scoped (everything else in the settings table
# is node-local: install_id, node_keys, cluster, dns_status, …).
CLUSTER_SETTINGS = ("webhook", "admin_domain")


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ───── export ─────────────────────────────────────────────────────────────────


async def export_state(session: AsyncSession, node_id: str) -> dict[str, Any]:
    integrations = (await session.execute(select(Integration))).scalars().all()
    domains = (await session.execute(select(Domain))).scalars().all()
    projects = (await session.execute(select(Project))).scalars().all()
    environments = (await session.execute(select(Environment))).scalars().all()
    services = (await session.execute(select(Service))).scalars().all()
    env_vars = (await session.execute(select(ServiceEnvVar))).scalars().all()
    identities = (await session.execute(select(Identity))).scalars().all()

    integ_by_id = {i.id: i for i in integrations}
    domain_by_id = {d.id: d for d in domains}
    project_by_id = {p.id: p for p in projects}
    env_by_id = {e.id: e for e in environments}
    svc_by_id = {s.id: s for s in services}

    out: dict[str, Any] = {
        "schema": SCHEMA,
        "node_id": node_id,
        "exported_at": datetime.utcnow().isoformat(),
        "integrations": [
            {
                "provider": i.provider, "account_login": i.account_login,
                "account_id": i.account_id, "name": i.name,
                "secret_encrypted": i.secret_encrypted, "config": i.config or {},
                "status": i.status, "updated_at": _iso(i.updated_at),
            }
            for i in integrations
        ],
        "domains": [
            {
                "name": d.name, "mode": d.mode, "is_primary": d.is_primary,
                "cloudflare_routed": d.cloudflare_routed,
                "zone_status": d.zone_status, "zone_id": d.zone_id,
                "name_servers": d.name_servers,
            }
            for d in domains
        ],
        "projects": [
            {
                "repo_full_name": p.repo_full_name, "name": p.name,
                "default_branch": p.default_branch,
                "integration": (
                    {"provider": integ_by_id[p.integration_id].provider,
                     "account_login": integ_by_id[p.integration_id].account_login}
                    if p.integration_id in integ_by_id else None
                ),
                "domain_name": domain_by_id[p.domain_id].name if p.domain_id in domain_by_id else None,
                "managed": p.managed, "auto_deploy": p.auto_deploy,
                "require_checks": p.require_checks, "description": p.description,
                "detected_stack": p.detected_stack or {},
                "dissected_at": _iso(p.dissected_at),
            }
            for p in projects
        ],
        "environments": [
            {
                "project_name": project_by_id[e.project_id].name,
                "name": e.name, "kind": e.kind, "branch": e.branch,
                "domain_name": domain_by_id[e.domain_id].name if e.domain_id in domain_by_id else None,
                "promotion_gate": e.promotion_gate, "e2e_workflow": e.e2e_workflow,
                "promote_from": (
                    env_by_id[e.promote_from_env_id].name
                    if e.promote_from_env_id in env_by_id else None
                ),
                "slug_suffix": e.slug_suffix, "is_default": e.is_default,
            }
            for e in environments if e.project_id in project_by_id
        ],
        "services": [
            {
                "project_name": project_by_id[s.project_id].name,
                "name": s.name, "kind": s.kind, "source_type": s.source_type,
                "source_ref": s.source_ref, "is_public": s.is_public,
                "subdomain_label": s.subdomain_label, "internal_port": s.internal_port,
                "depends_on": s.depends_on or [], "env_template": s.env_template or {},
            }
            for s in services if s.project_id in project_by_id
        ],
        "service_env_vars": [
            {
                "project_name": project_by_id[svc_by_id[v.service_id].project_id].name,
                "service_name": svc_by_id[v.service_id].name,
                "environment_name": (
                    env_by_id[v.environment_id].name
                    if v.environment_id and v.environment_id in env_by_id else None
                ),
                "key": v.key, "value": v.value, "source": v.source,
                "is_secret": v.is_secret,
            }
            for v in env_vars
            if v.service_id in svc_by_id and svc_by_id[v.service_id].project_id in project_by_id
        ],
        "identities": [{"email": i.email, "enabled": i.enabled} for i in identities],
        "settings": {},
        "deployments": [],
    }

    for key in CLUSTER_SETTINGS:
        row = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
        if row is not None:
            out["settings"][key] = row.value

    # Latest deployment per environment (for reconcile, not history sync).
    for e in environments:
        if e.project_id not in project_by_id:
            continue
        latest = (await session.execute(
            select(Deployment).where(Deployment.environment_id == e.id)
            .order_by(Deployment.created_at.desc()).limit(1)
        )).scalar_one_or_none()
        if latest is None:
            continue
        out["deployments"].append({
            "project_name": project_by_id[e.project_id].name,
            "env_name": e.name,
            "status": latest.status,
            "commit_sha": latest.commit_sha,
            "updated_at": _iso(latest.updated_at),
        })
    return out


# ───── import ─────────────────────────────────────────────────────────────────


async def import_state(session: AsyncSession, data: dict[str, Any], *, mode: str) -> dict[str, int]:
    """Upsert an exported state into the local DB. `mode`: full | deploy | update
    (see module docstring). Returns counters for logging."""
    overwrite = mode in ("full", "deploy")
    counts = {"added": 0, "updated": 0}

    def bump(created: bool) -> None:
        counts["added" if created else "updated"] += 1

    # integrations — natural key (provider, account_login); newer-wins always
    # (they carry updated_at).
    integ_map: dict[tuple, Integration] = {}
    for row in (await session.execute(select(Integration))).scalars():
        integ_map[(row.provider, row.account_login)] = row
    for item in data.get("integrations") or []:
        key = (item["provider"], item.get("account_login"))
        local = integ_map.get(key)
        incoming_ts = _parse_dt(item.get("updated_at"))
        if local is None:
            local = Integration(provider=item["provider"], account_login=item.get("account_login"))
            session.add(local)
            integ_map[key] = local
            bump(True)
        elif overwrite or (incoming_ts and local.updated_at and incoming_ts > local.updated_at):
            bump(False)
        else:
            continue
        local.account_id = item.get("account_id")
        local.name = item.get("name")
        local.secret_encrypted = item.get("secret_encrypted")
        local.config = item.get("config") or {}
        local.status = item.get("status") or "connected"
        if incoming_ts:
            local.updated_at = incoming_ts
    await session.commit()

    # domains — natural key: name
    domain_map: dict[str, Domain] = {
        d.name: d for d in (await session.execute(select(Domain))).scalars()
    }
    saw_primary = False
    for item in data.get("domains") or []:
        local = domain_map.get(item["name"])
        if local is None:
            local = Domain(name=item["name"])
            session.add(local)
            domain_map[item["name"]] = local
            bump(True)
        elif not overwrite:
            continue
        else:
            bump(False)
        local.mode = item.get("mode") or "wildcard"
        local.is_primary = bool(item.get("is_primary"))
        saw_primary = saw_primary or local.is_primary
        local.cloudflare_routed = bool(item.get("cloudflare_routed"))
        local.zone_status = item.get("zone_status") or "active"
        local.zone_id = item.get("zone_id")
        local.name_servers = item.get("name_servers")
    if overwrite and saw_primary:
        # Exactly one primary: the exporter's choice wins.
        exported_primary = next(
            (d["name"] for d in data.get("domains") or [] if d.get("is_primary")), None,
        )
        for d in domain_map.values():
            d.is_primary = d.name == exported_primary
    await session.commit()

    # projects — natural key: repo_full_name
    project_map: dict[str, Project] = {
        p.repo_full_name: p for p in (await session.execute(select(Project))).scalars()
    }
    names_in_use = {p.name for p in project_map.values()}
    for item in data.get("projects") or []:
        local = project_map.get(item["repo_full_name"])
        if local is None:
            # Respect Project.name uniqueness if an unrelated local project
            # already took the slug (shouldn't happen in a synced cluster).
            name = item["name"]
            if name in names_in_use:
                name = f"{name}-x"
            local = Project(repo_full_name=item["repo_full_name"], name=name)
            session.add(local)
            project_map[item["repo_full_name"]] = local
            names_in_use.add(name)
            bump(True)
        elif not overwrite:
            continue
        else:
            bump(False)
        integ_ref = item.get("integration") or {}
        integ = integ_map.get((integ_ref.get("provider"), integ_ref.get("account_login")))
        local.integration_id = integ.id if integ else local.integration_id
        dom = domain_map.get(item.get("domain_name") or "")
        local.domain_id = dom.id if dom else None
        local.default_branch = item.get("default_branch") or "main"
        local.managed = bool(item.get("managed"))
        local.auto_deploy = bool(item.get("auto_deploy"))
        local.require_checks = bool(item.get("require_checks"))
        local.description = item.get("description")
        local.detected_stack = item.get("detected_stack") or {}
        local.dissected_at = _parse_dt(item.get("dissected_at"))
    await session.commit()

    # environments — natural key: (project, name); promote_from second pass
    env_map: dict[tuple, Environment] = {}
    project_by_id = {p.id: p for p in project_map.values()}
    for row in (await session.execute(select(Environment))).scalars():
        proj = project_by_id.get(row.project_id)
        if proj:
            env_map[(proj.name, row.name)] = row
    project_by_name = {p.name: p for p in project_map.values()}
    for item in data.get("environments") or []:
        proj = project_by_name.get(item["project_name"])
        if not proj:
            continue
        key = (proj.name, item["name"])
        local = env_map.get(key)
        if local is None:
            local = Environment(project_id=proj.id, name=item["name"])
            session.add(local)
            env_map[key] = local
            bump(True)
        elif not overwrite:
            continue
        else:
            bump(False)
        local.kind = item.get("kind") or "dev"
        local.branch = item.get("branch")
        dom = domain_map.get(item.get("domain_name") or "")
        local.domain_id = dom.id if dom else None
        local.promotion_gate = bool(item.get("promotion_gate"))
        local.e2e_workflow = item.get("e2e_workflow")
        local.slug_suffix = item.get("slug_suffix") or ""
        local.is_default = bool(item.get("is_default"))
    await session.commit()
    for item in data.get("environments") or []:
        if not item.get("promote_from"):
            continue
        local = env_map.get((item["project_name"], item["name"]))
        source = env_map.get((item["project_name"], item["promote_from"]))
        if local is not None and source is not None and local.promote_from_env_id != source.id:
            local.promote_from_env_id = source.id
    await session.commit()

    # services — natural key: (project, name)
    svc_map: dict[tuple, Service] = {}
    for row in (await session.execute(select(Service))).scalars():
        proj = project_by_id.get(row.project_id)
        if proj:
            svc_map[(proj.name, row.name)] = row
    for item in data.get("services") or []:
        proj = project_by_name.get(item["project_name"])
        if not proj:
            continue
        key = (proj.name, item["name"])
        local = svc_map.get(key)
        if local is None:
            local = Service(project_id=proj.id, name=item["name"])
            session.add(local)
            svc_map[key] = local
            bump(True)
        elif not overwrite:
            continue
        else:
            bump(False)
        local.kind = item.get("kind") or "other"
        local.source_type = item.get("source_type") or "compose"
        local.source_ref = item.get("source_ref")
        local.is_public = bool(item.get("is_public"))
        local.subdomain_label = item.get("subdomain_label") or ""
        local.internal_port = item.get("internal_port")
        local.depends_on = item.get("depends_on") or []
        local.env_template = item.get("env_template") or {}
    await session.commit()

    # env vars — natural key: (service, environment-or-None, key)
    var_map: dict[tuple, ServiceEnvVar] = {}
    svc_by_id = {s.id: s for s in svc_map.values()}
    env_by_id = {e.id: e for e in env_map.values()}
    for row in (await session.execute(select(ServiceEnvVar))).scalars():
        svc = svc_by_id.get(row.service_id)
        if not svc:
            continue
        proj = project_by_id.get(svc.project_id)
        envname = env_by_id[row.environment_id].name if row.environment_id in env_by_id else None
        if proj:
            var_map[(proj.name, svc.name, envname, row.key)] = row
    for item in data.get("service_env_vars") or []:
        svc = svc_map.get((item["project_name"], item["service_name"]))
        if not svc:
            continue
        envname = item.get("environment_name")
        env = env_map.get((item["project_name"], envname)) if envname else None
        if envname and env is None:
            continue
        key = (item["project_name"], item["service_name"], envname, item["key"])
        local = var_map.get(key)
        if local is None:
            local = ServiceEnvVar(
                service_id=svc.id, environment_id=env.id if env else None, key=item["key"],
            )
            session.add(local)
            var_map[key] = local
            bump(True)
        elif not overwrite:
            continue
        else:
            bump(False)
        local.value = item.get("value") or ""
        local.source = item.get("source") or "user"
        local.is_secret = bool(item.get("is_secret"))
    await session.commit()

    # identities — natural key: email
    id_map = {i.email: i for i in (await session.execute(select(Identity))).scalars()}
    for item in data.get("identities") or []:
        email = (item.get("email") or "").strip().lower()
        if not email:
            continue
        local = id_map.get(email)
        if local is None:
            session.add(Identity(email=email, enabled=bool(item.get("enabled", True))))
            bump(True)
        elif overwrite and local.enabled != bool(item.get("enabled", True)):
            local.enabled = bool(item.get("enabled", True))
            bump(False)
    await session.commit()

    # cluster-scoped settings — fill-if-missing; overwrite on full/deploy
    for key in CLUSTER_SETTINGS:
        if key not in (data.get("settings") or {}):
            continue
        incoming = data["settings"][key]
        row = (await session.execute(select(Setting).where(Setting.key == key))).scalar_one_or_none()
        if row is None:
            session.add(Setting(key=key, value=incoming))
            bump(True)
        elif overwrite and row.value != incoming:
            row.value = incoming
            bump(False)
    await session.commit()

    log.info("cluster sync import (%s): +%d added, %d updated",
             mode, counts["added"], counts["updated"])
    return counts
