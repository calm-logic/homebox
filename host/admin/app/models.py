"""Homebox admin data model.

The model is staged around how Homebox actually thinks about deployments:

    Integration  — a connection to an external system (GitHub / GitLab /
                   Cloudflare). One row per provider account. Holds the
                   credential (encrypted) + non-secret provider state.
    Project      — 1:1 with a source-control repository. The unit a user
                   "adopts" and Homebox deploys.
    Environment  — a deploy target within a project (production, dev, and
                   later feature/preview). Defines the hostname suffix.
    Service      — a single container/app within a project (web, api, db,
                   cache, worker…). Discovered by dissection (app/dissect.py).
    ServiceEnvVar— env vars for a service, including the connection vars
                   (DATABASE_URL, REDIS_URL…) auto-wired during dissection.
    Deployment   — one deploy event for a (project, environment). Our own
                   "workflow run".
    ServiceInstance — the running container + URL for a service in a deploy.

Plus Domain (routable Cloudflare host), MetricSample / UptimeSample (sampled by
the background tasks), WorkflowRunCache (GitHub Actions cache), Setting (misc
key/value), and Identity (passwordless-login whitelist).
"""

from datetime import datetime
from sqlalchemy import (
    String, Integer, BigInteger, Float, DateTime, ForeignKey, Text, JSON,
    Boolean, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base

# Placeholder the API returns instead of a secret env var's real value. The
# env-var save path treats an incoming value equal to this as "unchanged" and
# preserves what's stored — otherwise editing any field on the service would
# write the mask back over the real secret (it corrupted a live Google OAuth
# secret to six bullet bytes exactly this way).
SECRET_MASK = "••••••"


class Integration(Base):
    """A connection to an external system Homebox depends on — one row per
    provider account. Replaces the old per-org PAT storage (Organization) and
    the ad-hoc Setting('cloudflare') blob.

    provider          github | gitlab | cloudflare
    secret_encrypted  primary credential (PAT / OAuth access token / API token)
                      encrypted at rest. GitHub OAuth tokens are tagged 'oauth:'.
    config            non-secret provider state as JSON. For cloudflare this is
                      the tunnel/account dict the app/cloudflare.py helpers
                      operate on (account_id, tunnel_id, connector_token_encrypted…).
    """
    __tablename__ = "integrations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    account_login: Mapped[str | None] = mapped_column(String(255), nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # display label
    secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(32), default="connected")  # connected | error | disconnected
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("provider", "account_login", name="uq_integration_provider_account"),
    )

    projects: Mapped[list["Project"]] = relationship(
        back_populates="integration", cascade="all, delete-orphan"
    )


class Project(Base):
    """1:1 with a source-control repository. Created (managed=False) for every
    repo we discover via an Integration; `managed` flips to True when the user
    adopts it, which auto-creates its environments + dissects its services."""
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    integration_id: Mapped[int | None] = mapped_column(
        ForeignKey("integrations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    repo_full_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)  # URL-safe slug
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    domain_id: Mapped[int | None] = mapped_column(
        ForeignKey("domains.id", ondelete="SET NULL"), nullable=True
    )
    # How this project's hostnames are shaped (see app/urls.py): container =
    # name-prefixed subdomains; base = this project owns the whole domain.
    domain_mode: Mapped[str] = mapped_column(String(32), default="container")
    managed: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_deploy: Mapped[bool] = mapped_column(Boolean, default=True)
    # Gate push auto-deploys on GitHub checks passing (no-op for repos with no
    # workflows — those deploy on push immediately).
    require_checks: Mapped[bool] = mapped_column(Boolean, default=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    detected_stack: Mapped[dict] = mapped_column(JSON, default=dict)  # last dissection snapshot
    dissected_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # When a USER last edited this row's config (adopt, domain, URL mode, …).
    # Cluster sync compares it for newer-wins conflict resolution, so it is
    # stamped ONLY by user-facing edit routes — never by derived-data refreshes
    # like dissection, which must not promote a stale config copy to "newest".
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    integration: Mapped["Integration | None"] = relationship(back_populates="projects")
    domain: Mapped["Domain | None"] = relationship()
    environments: Mapped[list["Environment"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )
    services: Mapped[list["Service"]] = relationship(
        back_populates="project", cascade="all, delete-orphan"
    )


class Environment(Base):
    """A deploy target within a project. `slug_suffix` is appended to the
    project name segment of every hostname for this env (see app/urls.py):
    production="", dev="--dev", a feature env="--<name>"."""
    __tablename__ = "environments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(64))            # production | dev | <feature>
    kind: Mapped[str] = mapped_column(String(16), default="dev")  # production | dev | preview
    branch: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Per-env override of the project's domain (null = inherit project/primary).
    domain_id: Mapped[int | None] = mapped_column(
        ForeignKey("domains.id", ondelete="SET NULL"), nullable=True
    )
    # Staged pipeline: when True this env is NOT deployed on push. Instead it
    # is promoted after `promote_from` (default: the project's dev env) deploys
    # the same commit successfully — and, if `e2e_workflow` is set, after that
    # workflow (dispatched against the source env's URL) passes too.
    promotion_gate: Mapped[bool] = mapped_column(Boolean, default=False)
    e2e_workflow: Mapped[str | None] = mapped_column(String(255), nullable=True)
    promote_from_env_id: Mapped[int | None] = mapped_column(
        ForeignKey("environments.id", ondelete="SET NULL"), nullable=True
    )
    slug_suffix: Mapped[str] = mapped_column(String(32), default="")
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # User-config edit timestamp — see Project.updated_at.
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_env_project_name"),)

    project: Mapped["Project"] = relationship(back_populates="environments")
    deployments: Mapped[list["Deployment"]] = relationship(
        back_populates="environment", cascade="all, delete-orphan"
    )


class Service(Base):
    """A single container/app within a project, discovered by dissection.
    `subdomain_label` builds the per-service hostname: ""→ the main UI (box),
    "api"→ box-api, "db"→ box-db. `is_public` services get a URL + Traefik
    route per environment."""
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(128))  # compose service name
    kind: Mapped[str] = mapped_column(String(16), default="other")  # web|api|database|cache|worker|static|other
    source_type: Mapped[str] = mapped_column(String(16), default="compose")  # compose|dockerfile|image|buildpack
    source_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    is_public: Mapped[bool] = mapped_column(Boolean, default=False)
    subdomain_label: Mapped[str] = mapped_column(String(64), default="")
    internal_port: Mapped[int | None] = mapped_column(Integer, nullable=True)
    depends_on: Mapped[list] = mapped_column(JSON, default=list)   # list[str] of service names
    env_template: Mapped[dict] = mapped_column(JSON, default=dict)  # static env declared in compose
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_service_project_name"),)

    project: Mapped["Project"] = relationship(back_populates="services")
    env_vars: Mapped[list["ServiceEnvVar"]] = relationship(
        back_populates="service", cascade="all, delete-orphan"
    )


class ServiceEnvVar(Base):
    """An environment variable for a service. `environment_id` NULL = applies to
    every environment. `source='auto'` rows are the connection vars wired by
    dissection (DATABASE_URL, REDIS_URL…); 'user' rows are edited in the UI."""
    __tablename__ = "service_env_vars"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), index=True
    )
    environment_id: Mapped[int | None] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), nullable=True
    )
    key: Mapped[str] = mapped_column(String(255))
    value: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[str] = mapped_column(String(16), default="user")  # auto | user
    is_secret: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    service: Mapped["Service"] = relationship(back_populates="env_vars")


class Deployment(Base):
    """One deploy event for a (project, environment) — our own workflow run."""
    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    environment_id: Mapped[int] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), index=True
    )
    # pending_checks (waiting on GitHub checks) | pending_promotion (waiting on
    # the source env's deploy) | pending_e2e (e2e workflow running against the
    # source env) | queued | cloning | dissecting |
    # building | starting | running | failed | stopped |
    # superseded (was running, replaced by a newer successful deploy) |
    # blocked (checks failed/timed out — never built)
    status: Mapped[str] = mapped_column(String(32), default="queued")
    stack_name: Mapped[str] = mapped_column(String(255))
    # Which cluster node ran this deploy (= install_id). Null on rows that
    # predate clustering; single-node installs just see their own id.
    node_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual | webhook
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    environment: Mapped["Environment"] = relationship(back_populates="deployments")
    instances: Mapped[list["ServiceInstance"]] = relationship(
        back_populates="deployment", cascade="all, delete-orphan"
    )


class ServiceInstance(Base):
    """The running container + URL for one service in one deployment."""
    __tablename__ = "service_instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    deployment_id: Mapped[int] = mapped_column(
        ForeignKey("deployments.id", ondelete="CASCADE"), index=True
    )
    service_id: Mapped[int | None] = mapped_column(
        ForeignKey("services.id", ondelete="SET NULL"), nullable=True
    )
    service_name: Mapped[str] = mapped_column(String(128))
    container_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    deployment: Mapped["Deployment"] = relationship(back_populates="instances")


class MetricSample(Base):
    """One resource sample for a running service container, written by the
    background sampler (app/metrics.py). net_rx/net_tx are CUMULATIVE byte
    counters straight from Docker; per-second rates are derived at query time."""
    __tablename__ = "metric_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(
        ForeignKey("services.id", ondelete="CASCADE"), index=True
    )
    environment_id: Mapped[int | None] = mapped_column(
        ForeignKey("environments.id", ondelete="CASCADE"), nullable=True, index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    cpu_pct: Mapped[float] = mapped_column(Float, default=0.0)
    mem_used: Mapped[int] = mapped_column(BigInteger, default=0)
    mem_limit: Mapped[int] = mapped_column(BigInteger, default=0)
    net_rx: Mapped[int] = mapped_column(BigInteger, default=0)  # cumulative bytes
    net_tx: Mapped[int] = mapped_column(BigInteger, default=0)  # cumulative bytes


class UptimeSample(Base):
    """One health observation for a piece of Homebox infrastructure, written by
    the background monitor (app/monitor.py). Components: 'tunnel' (the tunnel's
    connection state at Cloudflare's edge), 'cloudflared' / 'traefik' /
    'docker_proxy' (local container running state), and 'admin_url' (end-to-end
    GET of the public admin URL through the tunnel)."""
    __tablename__ = "uptime_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    component: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16))  # up | degraded | down | unknown
    detail: Mapped[str | None] = mapped_column(String(512), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class Domain(Base):
    """A routable host managed via Cloudflare. Per-service hostnames are derived
    (app/urls.py), not stored — only the roots live here."""
    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    cloudflare_routed: Mapped[bool] = mapped_column(Boolean, default=False)
    # Zone lifecycle for domains created in Cloudflare by Homebox:
    # active (default) | pending (zone created, waiting on registrar NS change)
    zone_status: Mapped[str] = mapped_column(String(16), default="active")
    zone_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    name_servers: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # User-config edit timestamp — see Project.updated_at.
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WorkflowRunCache(Base):
    __tablename__ = "workflow_runs_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True, index=True
    )
    repository_full_name: Mapped[str] = mapped_column(String(255), index=True)
    run_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(64))
    conclusion: Mapped[str | None] = mapped_column(String(64), nullable=True)
    head_branch: Mapped[str] = mapped_column(String(255))
    html_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict] = mapped_column(JSON, default=dict)


class Identity(Base):
    """A whitelisted email allowed to sign into the admin passwordlessly via
    OAuth (Google or GitHub). Login activity is tracked so the Identities page
    can show who has actually used their access."""
    __tablename__ = "identities"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)  # stored lowercased
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_login_provider: Mapped[str | None] = mapped_column(String(16), nullable=True)  # github | google
    login_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
