"""DB-side helpers for per-service deployment targets (no provider I/O here —
that lives in app/targets/*; this module answers "where does each service go,
who executes cloud sections, and which hostnames are cloud-routed").

Resolution convention (mirrors ServiceEnvVar): a ServiceTarget row with
environment_id NULL is the service-wide default; an env-specific row overrides
it; no row at all means the homebox default.

Coordinator rule: cloud-target operations must run ONCE per cluster, not once
per node. Every node still deploys the homebox portion of a stack; the node
with the LOWEST ordinal among fresh, serving, non-mirror roster entries also
executes the cloud sections. Mirrors never coordinate (they never originate
deploys either — clusterlib.fanout_deploy). Single-node installs always
coordinate. The election is deterministic from shared roster state, so a
brief disagreement during roster staleness at worst double-runs an idempotent
create-or-update against deterministically-named resources.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Environment, Integration, Project, Service, ServiceTarget
from .targets import variant_for
from .targets.base import ProxyRule

log = logging.getLogger("homebox.targets")

VALID_TARGETS = ("homebox", "aws", "gcp", "cloudflare")
PROJECT_TARGETS = ("automatic",) + VALID_TARGETS
PROJECT_DEFAULT_MARKER = "_project_default"

# Container-serverless variants: no persistent network, so a homebox-hosted
# DB/cache producer is reached via tunnel TCP ingress + Access (phase 4).
SERVERLESS_VARIANTS = ("cloud_run", "app_runner")

# Database-VM variants (phase 5). These are ADDITIVE: the local replicated
# container stays in the compose (homebox nodes keep active-active copies)
# while deploy._provision_db_vms brings up the VM as an extra Spock node.
DB_VM_VARIANTS = ("ec2_db", "gce_db")

# Producer kinds a serverless consumer can dial through the tunnel, and the
# container-side port the tunnel ingress connects to.
DB_TUNNEL_PORTS = {"database": 5432, "cache": 6379}

# First 127.0.0.1 port the baked-in cloudflared access proxy listens on inside
# a wrapped serverless image; producers get base+index (sorted by name).
DB_PROXY_BASE_PORT = 15432


@dataclass
class ResolvedTarget:
    """One service's effective target for one environment."""
    target: str = "homebox"
    variant: str | None = None
    integration_id: int | None = None
    config: dict[str, Any] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)
    row_id: int | None = None          # the winning ServiceTarget row (None = implicit homebox)
    # Homebox targets only (linked accounts, D3): WHERE in the account this
    # service runs — {"cluster_id": ...} or {"node_id": ...} (standalone
    # cluster-of-one), lifted from the row's config. None = "this homebox",
    # the unlinked/legacy meaning.
    location: dict[str, Any] | None = None

    @property
    def cloud(self) -> bool:
        return self.target != "homebox"


def _location_from_config(target: str, config: dict[str, Any] | None) -> dict[str, Any] | None:
    """The homebox location carried in a ServiceTarget config, or None."""
    if target != "homebox":
        return None
    cfg = config or {}
    if cfg.get("cluster_id"):
        return {"cluster_id": cfg["cluster_id"]}
    if cfg.get("node_id"):
        return {"node_id": cfg["node_id"]}
    return None


async def effective_targets(
    session: AsyncSession, project: Project, env: Environment,
) -> dict[str, ResolvedTarget]:
    """service name -> resolved target for this (project, environment).
    Env-specific row beats the NULL default row beats implicit homebox."""
    rows = (await session.execute(
        select(ServiceTarget, Service.name, Service.kind)
        .join(Service, Service.id == ServiceTarget.service_id)
        .where(Service.project_id == project.id)
        .where((ServiceTarget.environment_id == env.id)
               | (ServiceTarget.environment_id.is_(None)))
    )).all()
    out: dict[str, ResolvedTarget] = {}
    # NULL rows first so env-specific rows overwrite them.
    for st, name, kind in sorted(rows, key=lambda r: r[0].environment_id is not None):
        out[name] = ResolvedTarget(
            target=st.target or "homebox",
            variant=variant_for(st.target or "homebox", kind, st.config),
            integration_id=st.integration_id,
            config=dict(st.config or {}),
            state=dict(st.state or {}),
            row_id=st.id,
            location=_location_from_config(st.target or "homebox", st.config),
        )
    return out


def _automatic_target(kind: str, env: Environment,
                      integrations: dict[str, Integration]) -> tuple[str, int | None]:
    """Pick a cost-aware target: development stays local; production uses a
    connected scale-to-zero/object-storage provider when that service kind is
    supported. Stateful caches/workers remain on Homebox."""
    if env.kind != "production":
        return "homebox", None
    for provider in ("cloudflare", "gcp", "aws"):
        integ = integrations.get(provider)
        if integ and variant_for(provider, kind) is not None:
            return provider, integ.id
    return "homebox", None


async def sync_project_target_rows(session: AsyncSession, project: Project) -> None:
    """Materialize a project's inherited target into ServiceTarget rows.

    The deploy engine already persists provider state on ServiceTarget, so
    generated rows (marked in config) preserve those semantics. Explicit rows
    are never overwritten. Project defaults are environment-specific so each
    environment retains independent provider state and can be overridden by a
    service row without losing teardown metadata.
    """
    services = list((await session.execute(
        select(Service).where(Service.project_id == project.id)
    )).scalars())
    envs = list((await session.execute(
        select(Environment).where(Environment.project_id == project.id)
    )).scalars())
    integrations = {
        i.provider: i for i in (await session.execute(
            select(Integration).where(Integration.status == "connected")
        )).scalars()
    }
    rows = list((await session.execute(
        select(ServiceTarget).join(Service).where(Service.project_id == project.id)
    )).scalars())
    generated = {
        (r.service_id, r.environment_id): r for r in rows
        if (r.config or {}).get(PROJECT_DEFAULT_MARKER)
    }
    explicit = {
        (r.service_id, r.environment_id) for r in rows
        if not (r.config or {}).get(PROJECT_DEFAULT_MARKER)
    }
    explicit_defaults = {sid for sid, eid in explicit if eid is None}
    desired: dict[tuple[int, int | None], tuple[str, int | None, dict]] = {}
    project_target = project.deployment_target or "homebox"

    for svc in services:
        if svc.id in explicit_defaults:
            continue
        for env in envs:
            env_id = env.id
            key = (svc.id, env_id)
            if key in explicit:
                continue
            if project_target == "automatic":
                target, integration_id = _automatic_target(svc.kind, env, integrations)
                config: dict = {}
            else:
                supported = project_target == "homebox" or variant_for(project_target, svc.kind) is not None
                target = project_target if supported else "homebox"
                integration_id = (project.deployment_target_integration_id
                                  if target != "homebox" else None)
                config = dict(project.deployment_target_config or {}) if target == project_target else {}
            config[PROJECT_DEFAULT_MARKER] = True
            desired[key] = (target, integration_id, config)

    for key, (target, integration_id, config) in desired.items():
        row = generated.pop(key, None)
        changed = (
            row is None or row.target != target or row.integration_id != integration_id
            or dict(row.config or {}) != config
        )
        if row is None:
            row = ServiceTarget(service_id=key[0], environment_id=key[1])
            session.add(row)
        elif row.target != target and row.target != "homebox":
            state = dict(row.state or {})
            if not state.get("previous"):
                state["previous"] = {
                    "target": row.target,
                    "state": {k: v for k, v in state.items() if k != "previous"},
                }
            row.state = state
            row.state_updated_at = datetime.utcnow()
        row.target = target
        row.integration_id = integration_id
        # Preserve machine-only provider configuration separately in state;
        # generated user config can be refreshed whenever the project changes.
        row.config = config
        row.updated_at = (datetime.utcnow() if project_target == "automatic" and changed
                          else project.updated_at)
    for stale in generated.values():
        await session.delete(stale)


def resolve_for(targets_map: dict[str, ResolvedTarget] | None, name: str) -> ResolvedTarget:
    """The effective target for a service name; implicit homebox when absent."""
    if targets_map and name in targets_map:
        return targets_map[name]
    return ResolvedTarget()


# ── homebox target locations (linked accounts, D3/D4) ─────────────────────────
#
# With a linked account the "homebox" target is cluster-scoped: a row's config
# may carry {"cluster_id"} or {"node_id"} (mutually exclusive). A cluster
# deploys a homebox-targeted service iff the location matches its own identity
# or is absent. Every node knows all metadata (vault/cluster sync); each
# cluster deploys only its subset.


async def local_location(session: AsyncSession) -> dict[str, Any]:
    """THIS install's identity for location matching:
    {"cluster_id": <id or None>, "node_id": <id>}."""
    from . import clusterlib
    state = await clusterlib.load_cluster(session)
    return {
        "cluster_id": state.get("cluster_id") if state else None,
        "node_id": await clusterlib.get_node_id(session),
    }


def location_is_local(location: dict[str, Any] | None,
                      identity: dict[str, Any] | None) -> bool:
    """Whether a homebox-target location means THIS cluster/node. Absent
    location = "this homebox" (legacy/unlinked meaning) — always local. A
    None identity (caller didn't resolve one) keeps the legacy behavior of
    deploying everything locally."""
    loc = location or {}
    if not loc.get("cluster_id") and not loc.get("node_id"):
        return True
    if identity is None:
        return True
    if loc.get("cluster_id"):
        return loc["cluster_id"] == identity.get("cluster_id")
    return loc.get("node_id") == identity.get("node_id")


async def is_local_homebox(session: AsyncSession, resolved: ResolvedTarget) -> bool:
    """True iff this service's effective target is the homebox target AND its
    location (if any) points at THIS cluster (clusterlib.load_cluster) or THIS
    node (clusterlib.get_node_id — standalone cluster-of-one)."""
    if resolved.target != "homebox":
        return False
    if not resolved.location:
        return True
    return location_is_local(resolved.location, await local_location(session))


async def foreign_homebox_hostnames(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """hostname -> {cname_target: None, proxied, target: "homebox",
    cluster_id|node_id} for every PUBLIC service homebox-targeted at a
    DIFFERENT cluster/node. Companion exclusion registry to
    cloud_routed_hostnames: the OWNING cluster writes ingress/DNS for these
    hosts, so this cluster's tunnel ingress push and DNS drift report/repair
    must never touch them (cname_target stays None — we don't know or manage
    the foreign cluster's tunnel target)."""
    from .models import Domain
    from . import urls

    rows = (await session.execute(
        select(ServiceTarget, Service)
        .join(Service, Service.id == ServiceTarget.service_id)
        .where(ServiceTarget.target == "homebox")
    )).all()
    candidates = [(st, svc) for st, svc in rows
                  if _location_from_config("homebox", st.config) and svc.is_public]
    if not candidates:
        return {}
    identity = await local_location(session)
    primary = (await session.execute(
        select(Domain).where(Domain.is_primary == True)  # noqa: E712
    )).scalars().first()

    out: dict[str, dict[str, Any]] = {}
    for st, svc in candidates:
        loc = _location_from_config("homebox", st.config)
        if location_is_local(loc, identity):
            continue
        project = await session.get(Project, svc.project_id)
        if not project or not project.managed:
            continue
        envs = (await session.execute(
            select(Environment).where(Environment.project_id == project.id)
        )).scalars().all()
        for env in envs:
            if st.environment_id is not None and env.id != st.environment_id:
                continue
            # An env-specific row may override this service back to local/cloud.
            targets_map = await effective_targets(session, project, env)
            res = resolve_for(targets_map, svc.name)
            if res.target != "homebox" or location_is_local(res.location, identity):
                continue
            domain = await session.get(Domain, env.domain_id) if env.domain_id else None
            if domain is None and project.domain_id:
                domain = await session.get(Domain, project.domain_id)
            if domain is None:
                domain = primary
            if domain is None:
                continue
            base = project.domain_mode == "base"
            host = urls.full_host(
                project.name, "" if base else (svc.subdomain_label or ""),
                env.slug_suffix or "", domain.name, base=base)
            out[host.lower()] = {
                "cname_target": None, "proxied": True, "target": "homebox",
                **(res.location or {}),
            }
    return out


# ── per-host DNS overrides (cross-cluster domain sharing, G12) ────────────────
#
# When THIS cluster serves a hostname under a domain whose apex/wildcard CNAME
# points at ANOTHER cluster's tunnel, deploy._ensure_domain_overrides writes a
# specific-host CNAME → OUR tunnel (specific beats wildcard at Cloudflare) and
# records it here so the ingress push, DNS drift report/repair and teardown
# all agree on what we own. Node-local bookkeeping — the settings table is not
# cluster-synced; it's written on the deploy coordinator and every consumer
# degrades safely without it (the wildcard ingress rules still cover the
# hosts, and the drift repair never repoints records that already match our
# tunnel).

DNS_OVERRIDES_KEY = "dns_overrides"


async def load_dns_overrides(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """hostname -> {domain, zone_id, cname_target, proxied, project, env,
    service, created_at} for every per-host override CNAME this install wrote
    on a foreign-owned domain."""
    from .models import Setting
    row = (await session.execute(
        select(Setting).where(Setting.key == DNS_OVERRIDES_KEY)
    )).scalar_one_or_none()
    value = row.value if row else None
    return dict(value) if isinstance(value, dict) else {}


async def save_dns_overrides(
    session: AsyncSession, overrides: dict[str, dict[str, Any]],
) -> None:
    from .models import Setting
    row = (await session.execute(
        select(Setting).where(Setting.key == DNS_OVERRIDES_KEY)
    )).scalar_one_or_none()
    if row is None:
        session.add(Setting(key=DNS_OVERRIDES_KEY, value=dict(overrides)))
    else:
        row.value = dict(overrides)
    await session.commit()


async def is_cloud_coordinator(session: AsyncSession, state: dict[str, Any] | None) -> bool:
    """Whether THIS node executes cloud-target sections for deploys/reconciles.
    See module docstring for the election rule."""
    from .config import settings
    if settings.node_role == "mirror":
        return False
    if not state or not state.get("roster"):
        return True  # single node / not clustered
    from . import clusterlib
    self_id = await clusterlib.get_node_id(session)
    candidates: list[tuple[int, str]] = []
    for n in state["roster"]:
        nid, ordinal = n.get("node_id"), n.get("ordinal")
        if not nid or not ordinal or clusterlib.roster_role(n) != "peer":
            continue
        if nid == self_id:
            candidates.append((int(ordinal), nid))
            continue
        if clusterlib._roster_fresh(n) and n.get("serving") is not False:
            candidates.append((int(ordinal), nid))
    if not candidates:
        return True  # roster useless — better to act than to strand cloud targets
    return min(candidates)[1] == self_id


async def cloud_routed_hostnames(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """hostname -> {cname_target, proxied, target} for every cloud target that
    has a DNS record recorded in its state. THE exclusion registry the DNS
    drift report/repair consult — a hostname listed here must NOT be repointed
    at the tunnel."""
    rows = (await session.execute(
        select(ServiceTarget).where(ServiceTarget.target != "homebox")
    )).scalars().all()
    out: dict[str, dict[str, Any]] = {}
    for st in rows:
        dns = (st.state or {}).get("dns") or {}
        host = dns.get("hostname")
        if host:
            out[host.lower()] = {
                "cname_target": dns.get("cname_target"),
                "proxied": dns.get("proxied", True),
                "target": st.target,
            }
    return out


def rewrite_cross_target_env(
    auto_env: dict[str, str],
    consumer: ResolvedTarget,
    producer_targets: dict[str, ResolvedTarget],
    foreign_hosts: dict[str, str] | None = None,
) -> dict[str, str]:
    """Rewrite dissect's auto-wired connection URLs (which use bare compose
    service names as hostnames — only resolvable on the local docker network)
    for cross-target consumer/producer pairs.

    v1 matrix (full mesh):
      homebox app  -> homebox DB : unchanged
      homebox app  -> cloud VM DB: producer state.mesh ip (10.77.x.y)
      homebox app  -> FOREIGN homebox svc: its public hostname (foreign_hosts —
                                   a service homebox-targeted at another
                                   cluster/node; the caller derives the host)
      serverless   -> homebox DB : 127.0.0.1:<access-proxy port> (wrapper baked
                                   into the image at build time — phase 4)
      serverless   -> cloud VM DB: VM public endpoint (sslmode enforced)
    Only the hostname[:port] segment of URL-shaped values is rewritten; the
    producer service name is matched against the URL host.
    """
    if not producer_targets and not foreign_hosts:
        return auto_env
    out = dict(auto_env)
    for key, value in auto_env.items():
        if "://" not in value:
            continue
        try:
            scheme, rest = value.split("://", 1)
            creds, _, hostpart = rest.rpartition("@")
            hostname = hostpart.split("/", 1)[0].split(":", 1)[0]
        except ValueError:
            continue
        if foreign_hosts and hostname in foreign_hosts:
            # Producer runs on ANOTHER homebox cluster/node — reach it at its
            # public hostname (same mechanism as cloud targets).
            endpoint = foreign_hosts[hostname]
            out[key] = value.replace(f"@{hostname}", f"@{endpoint}") if creds \
                else value.replace(f"://{hostname}", f"://{endpoint}")
            continue
        prod = producer_targets.get(hostname)
        if prod is None or not prod.cloud:
            continue  # producer on homebox: consumer-side handling is phase 4
        if consumer.variant in SERVERLESS_VARIANTS and prod.variant in DB_VM_VARIANTS:
            # Serverless consumers have no WireGuard, so a DB VM is dialed at
            # its public IP — the VM's security group restricts 5432, and the
            # link is forced onto TLS.
            endpoint = (prod.state or {}).get("endpoint")
            if not endpoint:
                log.warning("cross-target env %s: producer %s has no public endpoint yet",
                            key, hostname)
                continue
            new = value.replace(f"@{hostname}", f"@{endpoint}") if creds \
                else value.replace(f"://{hostname}", f"://{endpoint}")
            if scheme.startswith("postgres") and "sslmode=" not in new:
                new += ("&" if "?" in new else "?") + "sslmode=require"
            out[key] = new
            continue
        endpoint = (prod.state or {}).get("mesh", {}).get("ip") \
            or (prod.state or {}).get("endpoint")
        if not endpoint:
            log.warning("cross-target env %s: producer %s has no endpoint yet", key, hostname)
            continue
        out[key] = value.replace(f"@{hostname}", f"@{endpoint}") if creds \
            else value.replace(f"://{hostname}", f"://{endpoint}")
    return out


async def reconcile_targets(session: AsyncSession, state: dict[str, Any] | None) -> int:
    """Coordinator-only self-heal for cloud targets: a target stuck in error —
    or half-provisioned with nothing having touched it for 15 minutes — gets
    its environment redeployed, which re-runs the idempotent cloud deploy.
    Called from the cluster reconcile loop; single-node installs run it too
    (they're always coordinator). Returns how many deploys were queued."""
    from datetime import datetime, timedelta
    if not await is_cloud_coordinator(session, state):
        return 0
    from .models import Deployment, Project

    rows = (await session.execute(
        select(ServiceTarget, Service)
        .join(Service, Service.id == ServiceTarget.service_id)
        .where(ServiceTarget.target != "homebox")
    )).all()
    queued_envs: set[int] = set()
    for st, svc in rows:
        s = st.state or {}
        # A live target can still have unfinished work: a Cloud Run custom
        # domain waiting on DNS-TXT site verification / mapping provisioning
        # (gcp_cloud_run) keeps serving from run.app until a re-deploy
        # completes the flow — retry those too. (Provider keys live nested
        # under resource_ids — see deploy._provider_state.)
        pending_domain = (s.get("resource_ids") or {}).get("domain_mapping") \
            in ("pending_verification", "pending_mapping")
        if s.get("status") == "live" and not pending_domain:
            continue
        ts = st.state_updated_at
        if ts and datetime.utcnow() - ts < timedelta(minutes=15):
            continue  # recent attempt (or in-flight deploy) — give it time
        project = await session.get(Project, svc.project_id)
        if not project or not project.managed:
            continue
        envs = (await session.execute(
            select(Environment).where(Environment.project_id == project.id)
        )).scalars().all()
        for env in envs:
            if st.environment_id is not None and env.id != st.environment_id:
                continue
            if env.id in queued_envs:
                continue
            latest = (await session.execute(
                select(Deployment).where(Deployment.environment_id == env.id)
                .order_by(Deployment.created_at.desc()).limit(1)
            )).scalar_one_or_none()
            # Only heal envs that HAVE been deployed and aren't mid-deploy.
            if latest is None or latest.status not in ("running", "failed"):
                continue
            from .clusterlib import _queue_cluster_deploy
            log.info("target reconcile: redeploying %s/%s (cloud target %s is %s)",
                     project.name, env.name, st.target, s.get("status") or "unset")
            await _queue_cluster_deploy(session, env)
            queued_envs.add(env.id)

    queued_envs |= await _reconcile_homebox_locations(session, queued_envs)
    return len(queued_envs)


async def _reconcile_homebox_locations(
    session: AsyncSession, queued_envs: set[int],
) -> set[int]:
    """Ownership-drift detection for LOCATED homebox targets (linked accounts):
    a retarget homebox@A → homebox@B lands on this cluster via sync, not via a
    local user action, so nothing queues the deploy that drops (or starts) the
    service here. Compare each located target's effective ownership against
    what this cluster's latest deploy actually did (ServiceInstance rows:
    status 'remote' / missing = not run here) and queue a redeploy of drifted
    envs. Best effort, coordinator-only (the caller gates). Returns the env
    ids queued."""
    from datetime import datetime, timedelta
    from .models import Deployment, ServiceInstance

    rows = (await session.execute(
        select(ServiceTarget, Service)
        .join(Service, Service.id == ServiceTarget.service_id)
        .where(ServiceTarget.target == "homebox")
    )).all()
    located = [(st, svc) for st, svc in rows
               if _location_from_config("homebox", st.config)]
    queued: set[int] = set()
    if not located:
        return queued
    identity = await local_location(session)
    seen_pairs: set[tuple[int, int]] = set()
    for st, svc in located:
        project = await session.get(Project, svc.project_id)
        if not project or not project.managed:
            continue
        envs = (await session.execute(
            select(Environment).where(Environment.project_id == project.id)
        )).scalars().all()
        for env in envs:
            if st.environment_id is not None and env.id != st.environment_id:
                continue
            if env.id in queued_envs or env.id in queued \
                    or (svc.id, env.id) in seen_pairs:
                continue
            seen_pairs.add((svc.id, env.id))
            targets_map = await effective_targets(session, project, env)
            res = resolve_for(targets_map, svc.name)
            if res.target != "homebox":
                continue  # env override to a cloud target: cloud loop's job
            is_local = location_is_local(res.location, identity)
            latest = (await session.execute(
                select(Deployment).where(Deployment.environment_id == env.id)
                .order_by(Deployment.created_at.desc()).limit(1)
            )).scalar_one_or_none()
            # Only heal envs that HAVE been deployed and aren't mid-deploy.
            if latest is None or latest.status not in ("running", "failed"):
                continue
            inst = (await session.execute(
                select(ServiceInstance).where(
                    ServiceInstance.deployment_id == latest.id,
                    ServiceInstance.service_name == svc.name)
            )).scalars().first()
            deployed_here = inst is not None and inst.status != "remote"
            if deployed_here == is_local:
                continue  # last deploy already reflects ownership
            # A deploy that ran AFTER this retarget and finished recently gets
            # the same 15-minute breather as cloud error retries (a failing
            # deploy shouldn't be requeued every reconcile tick).
            changed_at = st.updated_at or st.created_at
            if latest.updated_at and changed_at \
                    and latest.updated_at >= changed_at \
                    and datetime.utcnow() - latest.updated_at < timedelta(minutes=15):
                continue
            from .clusterlib import _queue_cluster_deploy
            log.info("target reconcile: redeploying %s/%s (homebox ownership "
                     "drift on %s: effective %s, deployed-here %s)",
                     project.name, env.name, svc.name,
                     "local" if is_local else "foreign", deployed_here)
            await _queue_cluster_deploy(session, env)
            queued.add(env.id)
    return queued


# ── cloud database VMs: mesh identity ─────────────────────────────────────────

# Reserved ordinal range for non-roster mesh members (cloud DB VMs). Control-
# plane roster ordinals count up from 1 and stay tiny; meshlib.mesh_ip is
# 16-bit, so the top 4K ordinals can never collide with a roster node.
MESH_ORDINAL_BASE = 0xF000


async def mesh_extra_peers(session: AsyncSession) -> list[dict[str, Any]]:
    """Cloud DB VMs that joined the WireGuard mesh: peer dicts for
    meshlib.build_conf(extra_peers=…) — {ordinal, wg_pubkey, endpoint}."""
    rows = (await session.execute(
        select(ServiceTarget).where(ServiceTarget.target != "homebox")
    )).scalars().all()
    out: list[dict[str, Any]] = []
    for st in rows:
        mesh = (st.state or {}).get("mesh") or {}
        if mesh.get("ordinal") and mesh.get("wg_pubkey"):
            out.append({
                "ordinal": int(mesh["ordinal"]),
                "wg_pubkey": mesh["wg_pubkey"],
                "endpoint": mesh.get("endpoint"),
                "ip": mesh.get("ip"),
            })
    return out


async def allocate_mesh_ordinal(session: AsyncSession) -> int:
    """Next free ordinal in the reserved DB-VM range. Coordinator-only (the
    caller gates); allocations are persisted into the target row's state.mesh
    before the VM exists, so a re-run reuses rather than reallocates."""
    used = {p["ordinal"] for p in await mesh_extra_peers(session)}
    ordinal = MESH_ORDINAL_BASE
    while ordinal in used:
        ordinal += 1
    if ordinal > 0xFFFF:
        raise RuntimeError("mesh ordinal space exhausted")
    return ordinal


# ── serverless → homebox DB path (tunnel TCP ingress + Access) ────────────────
#
# A Cloud Run / App Runner consumer has no route onto the homebox docker
# network, so its DB/cache producers are exposed as tunnel TCP ingress rules
# on deterministic hostnames, fronted by a Cloudflare Access app that only
# admits the cluster's service token (cloudflare.ensure_access_tcp_app), and
# dialed from inside the serverless container by a baked-in
# `cloudflared access tcp` proxy (targets/artifacts.wrap_with_access_proxy).


def _parse_url_host(value: str) -> tuple[str, str, bool] | None:
    """(host, host[:port], has_credentials) of a URL-shaped env value, or None.
    Same parsing approach as rewrite_cross_target_env."""
    if "://" not in value:
        return None
    rest = value.split("://", 1)[1]
    creds, _, hostpart = rest.rpartition("@")
    hostport = hostpart.split("/", 1)[0]
    host = hostport.split(":", 1)[0]
    if not host:
        return None
    return host, hostport, bool(creds)


def db_tunnel_rule(
    project_name: str, env_name: str, producer_name: str, kind: str,
    domain_name: str, stack: str,
) -> dict[str, str]:
    """THE single derivation of one homebox DB/cache producer's tunnel TCP
    ingress rule: {hostname, service}. Used by BOTH serverless_db_plan (deploy
    time — wrapper images + env overrides reference the hostname) and
    all_tunnel_tcp_rules (ingress-push time), so the two halves can never
    drift apart."""
    port = DB_TUNNEL_PORTS.get(kind, DB_TUNNEL_PORTS["database"])
    label = re.sub(r"[^a-z0-9-]", "-",
                   f"db-{project_name}-{producer_name}-{env_name}".lower())
    return {
        "hostname": f"{label}.{domain_name}",
        "service": f"tcp://{stack}-{producer_name}-1:{port}",
    }


async def serverless_db_plan(
    session: AsyncSession, project: Project, env: Environment,
    targets_map: dict[str, ResolvedTarget],
    detected_by_name: dict[str, Any], domain_name: str,
) -> dict[str, Any]:
    """Plan the serverless→homebox DB path for one (project, environment).

    For every consumer whose resolved variant is serverless (cloud_run /
    app_runner), scan its dissected auto_env connection URLs; each one whose
    host is a sibling service on the HOMEBOX target with a database/cache kind
    yields three coordinated pieces:

      proxy_rules[consumer]  — ProxyRule list baked into the wrapper image
                               (artifacts.wrap_with_access_proxy spawns one
                               `cloudflared access tcp` per rule);
      env_overrides[consumer]— the URL rewritten to 127.0.0.1:<local_port>,
                               where the in-container proxy listens;
      tcp_rules              — deduped {hostname, service} ingress entries the
                               tunnel needs (cf.build_ingress prepends them;
                               all_tunnel_tcp_rules re-derives the same set
                               from persisted state via db_tunnel_rule).

    Local ports are deterministic: DB_PROXY_BASE_PORT + index of the producer
    in the name-sorted set of referenced producers. Cloud-VM producers are NOT
    included — they're reached over the mesh/public endpoint
    (rewrite_cross_target_env)."""
    from . import urls
    stack = urls.stack_name(project, env)
    identity = await local_location(session)

    # First pass: find every (consumer, env key, producer) reference.
    refs: list[tuple[str, str, str, str]] = []  # (consumer, key, value, producer)
    producers: set[str] = set()
    for name, d in detected_by_name.items():
        resolved = resolve_for(targets_map, name)
        if resolved.variant not in SERVERLESS_VARIANTS:
            continue
        for key, value in (getattr(d, "auto_env", None) or {}).items():
            parsed = _parse_url_host(value)
            if not parsed:
                continue
            host = parsed[0]
            prod = detected_by_name.get(host)
            if prod is None or prod.kind not in DB_TUNNEL_PORTS:
                continue
            prod_res = resolve_for(targets_map, host)
            if prod_res.target != "homebox":
                continue  # cloud producer: mesh/endpoint path, not the tunnel
            if not location_is_local(prod_res.location, identity):
                continue  # foreign-homebox producer: not behind OUR tunnel
            refs.append((name, key, value, host))
            producers.add(host)

    ports = {p: DB_PROXY_BASE_PORT + i for i, p in enumerate(sorted(producers))}
    plan: dict[str, Any] = {"proxy_rules": {}, "env_overrides": {}, "tcp_rules": []}
    seen_hosts: set[str] = set()
    for consumer, key, value, producer in refs:
        rule = db_tunnel_rule(project.name, env.name, producer,
                              detected_by_name[producer].kind, domain_name, stack)
        local_port = ports[producer]
        rules = plan["proxy_rules"].setdefault(consumer, [])
        if all(r.hostname != rule["hostname"] for r in rules):
            rules.append(ProxyRule(hostname=rule["hostname"], local_port=local_port))
        _, hostport, has_creds = _parse_url_host(value)
        replacement = f"127.0.0.1:{local_port}"
        plan["env_overrides"].setdefault(consumer, {})[key] = (
            value.replace(f"@{hostport}", f"@{replacement}") if has_creds
            else value.replace(f"://{hostport}", f"://{replacement}")
        )
        if rule["hostname"] not in seen_hosts:
            seen_hosts.add(rule["hostname"])
            plan["tcp_rules"].append(rule)
    return plan


async def all_tunnel_tcp_rules(session: AsyncSession) -> list[dict[str, str]]:
    """Every tunnel TCP ingress rule the cluster currently needs, re-derived
    from PERSISTED state (ServiceTarget rows + source='auto' ServiceEnvVar
    connection URLs — no repo checkout/dissection, so the ingress-push routes
    can call this cheaply). Must agree with serverless_db_plan: both go
    through db_tunnel_rule. Deduped by hostname.

    Domain precedence per environment matches deploy: env override → project
    setting → primary Domain. Envs with no resolvable domain are skipped."""
    from .models import Domain, ServiceEnvVar
    from . import urls

    rows = (await session.execute(
        select(ServiceTarget, Service)
        .join(Service, Service.id == ServiceTarget.service_id)
        .where(ServiceTarget.target != "homebox")
    )).all()
    if not rows:
        return []

    primary = (await session.execute(
        select(Domain).where(Domain.is_primary == True)  # noqa: E712
    )).scalars().first()
    identity = await local_location(session)

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for st, svc in rows:
        if variant_for(st.target or "homebox", svc.kind, st.config) \
                not in SERVERLESS_VARIANTS:
            continue
        project = await session.get(Project, svc.project_id)
        if not project or not project.managed:
            continue
        siblings = (await session.execute(
            select(Service).where(Service.project_id == project.id)
        )).scalars().all()
        sib_by_name = {s.name: s for s in siblings}
        envs = (await session.execute(
            select(Environment).where(Environment.project_id == project.id)
        )).scalars().all()
        for env in envs:
            if st.environment_id is not None and env.id != st.environment_id:
                continue
            targets_map = await effective_targets(session, project, env)
            if resolve_for(targets_map, svc.name).variant not in SERVERLESS_VARIANTS:
                continue  # an env-specific row overrode this consumer back
            domain = await session.get(Domain, env.domain_id) if env.domain_id else None
            if domain is None and project.domain_id:
                domain = await session.get(Domain, project.domain_id)
            if domain is None:
                domain = primary
            if domain is None:
                continue
            stack = urls.stack_name(project, env)
            env_vars = (await session.execute(
                select(ServiceEnvVar)
                .where(ServiceEnvVar.service_id == svc.id)
                .where(ServiceEnvVar.source == "auto")
                .where((ServiceEnvVar.environment_id == env.id)
                       | (ServiceEnvVar.environment_id.is_(None)))
            )).scalars().all()
            for ev in env_vars:
                parsed = _parse_url_host(ev.value or "")
                if not parsed:
                    continue
                prod = sib_by_name.get(parsed[0])
                if prod is None or prod.kind not in DB_TUNNEL_PORTS:
                    continue
                prod_res = resolve_for(targets_map, prod.name)
                if prod_res.target != "homebox" \
                        or not location_is_local(prod_res.location, identity):
                    continue  # cloud or foreign-homebox producer: not OUR tunnel
                rule = db_tunnel_rule(project.name, env.name, prod.name,
                                      prod.kind, domain.name, stack)
                if rule["hostname"] not in seen:
                    seen.add(rule["hostname"])
                    out.append(rule)
    return out


async def db_vm_extra_nodes(session: AsyncSession, project: Project,
                            env: Environment, service_name: str) -> list[dict[str, Any]]:
    """Spock extra_nodes entries (cluster_db.ensure_replication) for cloud DB
    VMs backing this (project, env, service): {ordinal, host: mesh ip, port,
    node_name}. Empty until the VM is provisioned (state.mesh set)."""
    targets = await effective_targets(session, project, env)
    resolved = targets.get(service_name)
    if not resolved or resolved.variant not in DB_VM_VARIANTS:
        return []
    mesh = (resolved.state or {}).get("mesh") or {}
    db = (resolved.state or {}).get("db") or {}
    if not mesh.get("ordinal") or not mesh.get("ip"):
        return []
    return [{
        "ordinal": int(mesh["ordinal"]),
        "host": mesh["ip"],
        "port": int(db.get("port") or 5432),
        "node_name": db.get("node_name") or f"n{mesh['ordinal']}",
    }]
