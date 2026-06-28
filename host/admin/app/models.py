from datetime import datetime
from sqlalchemy import String, Integer, BigInteger, Float, DateTime, ForeignKey, Text, JSON, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .db import Base


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    pat_encrypted: Mapped[str] = mapped_column(Text)
    runner_registered: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    repositories: Mapped[list["Repository"]] = relationship(
        back_populates="organization", cascade="all, delete-orphan"
    )


class Repository(Base):
    __tablename__ = "repositories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    organization_id: Mapped[int | None] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=True
    )
    full_name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    default_branch: Mapped[str] = mapped_column(String(255), default="main")
    project_slug: Mapped[str | None] = mapped_column(String(255), nullable=True)
    homebox_ready: Mapped[bool] = mapped_column(Boolean, default=False)
    # Whether Homebox manages (deploys) this repo. Added to an existing table, so
    # backfilled by a startup ALTER in main.py (create_all won't add columns).
    managed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    organization: Mapped[Organization | None] = relationship(back_populates="repositories")
    deployments: Mapped[list["Deployment"]] = relationship(
        back_populates="repository", cascade="all, delete-orphan"
    )


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    slug: Mapped[str] = mapped_column(String(255), index=True)
    # queued | cloning | building | starting | running | failed | stopped
    status: Mapped[str] = mapped_column(String(32), default="queued")
    stack_name: Mapped[str] = mapped_column(String(255))
    web_container: Mapped[str | None] = mapped_column(String(255), nullable=True)
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger: Mapped[str] = mapped_column(String(16), default="manual")  # manual | webhook
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    repository: Mapped["Repository"] = relationship(back_populates="deployments")


class MetricSample(Base):
    """One resource sample for a managed project's web container, written by the
    background sampler (app/metrics.py). net_rx/net_tx are CUMULATIVE byte
    counters straight from Docker; per-second rates are derived at query time by
    diffing consecutive samples."""
    __tablename__ = "metric_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    repository_id: Mapped[int] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE"), index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    cpu_pct: Mapped[float] = mapped_column(Float, default=0.0)
    mem_used: Mapped[int] = mapped_column(BigInteger, default=0)
    mem_limit: Mapped[int] = mapped_column(BigInteger, default=0)
    net_rx: Mapped[int] = mapped_column(BigInteger, default=0)  # cumulative bytes
    net_tx: Mapped[int] = mapped_column(BigInteger, default=0)  # cumulative bytes


class Domain(Base):
    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    mode: Mapped[str] = mapped_column(String(32), default="wildcard")  # wildcard | dedicated
    project_slug: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    cloudflare_routed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WorkflowRunCache(Base):
    __tablename__ = "workflow_runs_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
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


class UptimeSample(Base):
    """One health observation for a piece of Homebox infrastructure, written by
    the background monitor (app/monitor.py). Components: 'tunnel' (the tunnel's
    connection state at Cloudflare's edge), 'cloudflared' / 'traefik' /
    'docker_proxy' (local container running state), and 'admin_url' (end-to-end
    GET of the public admin URL through the tunnel). Used to compute uptime % and
    a status timeline on the Tunnel page."""
    __tablename__ = "uptime_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    component: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(16))  # up | degraded | down | unknown
    detail: Mapped[str | None] = mapped_column(String(512), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


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
