"""baseline — the admin schema as of Alembic adoption

Revision ID: 0001
Revises:
Create Date: 2026-07-12

This is a FROZEN snapshot of the schema that the pre-Alembic bootstrap
(create_all + the additive ADD COLUMN block in app/main.py) produced. It is
built from a local MetaData literal — NOT app.models — so it keeps describing
this exact schema even as the models evolve. Fresh databases build from here;
pre-Alembic databases are reconciled to this shape and stamped at 0001 by
app.migrate before any later revision runs.

The four columns the old bootstrap added as NOT NULL ... DEFAULT (require_checks,
promotion_gate, zone_status, domain_mode) carry the same server_default here so
a fresh 0001 build matches an adopted legacy DB exactly.
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _metadata() -> sa.MetaData:
    m = sa.MetaData()

    sa.Table(
        "integrations", m,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("provider", sa.String(32), index=True, nullable=False),
        sa.Column("account_login", sa.String(255), nullable=True),
        sa.Column("account_id", sa.String(255), nullable=True),
        sa.Column("name", sa.String(255), nullable=True),
        sa.Column("secret_encrypted", sa.Text, nullable=True),
        sa.Column("config", sa.JSON, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("last_verified_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("provider", "account_login", name="uq_integration_provider_account"),
    )

    sa.Table(
        "domains", m,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(255), unique=True, index=True, nullable=False),
        sa.Column("is_primary", sa.Boolean, nullable=False),
        sa.Column("cloudflare_routed", sa.Boolean, nullable=False),
        sa.Column("zone_status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("zone_id", sa.String(64), nullable=True),
        sa.Column("name_servers", sa.JSON, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    sa.Table(
        "projects", m,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("integration_id", sa.Integer,
                  sa.ForeignKey("integrations.id", ondelete="SET NULL"),
                  index=True, nullable=True),
        sa.Column("repo_full_name", sa.String(255), unique=True, index=True, nullable=False),
        sa.Column("name", sa.String(255), unique=True, index=True, nullable=False),
        sa.Column("default_branch", sa.String(255), nullable=False),
        sa.Column("domain_id", sa.Integer,
                  sa.ForeignKey("domains.id", ondelete="SET NULL"), nullable=True),
        sa.Column("domain_mode", sa.String(32), nullable=False, server_default="container"),
        sa.Column("managed", sa.Boolean, nullable=False),
        sa.Column("auto_deploy", sa.Boolean, nullable=False),
        sa.Column("require_checks", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("detected_stack", sa.JSON, nullable=False),
        sa.Column("dissected_at", sa.DateTime, nullable=True),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    sa.Table(
        "environments", m,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("project_id", sa.Integer,
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("branch", sa.String(255), nullable=True),
        sa.Column("domain_id", sa.Integer,
                  sa.ForeignKey("domains.id", ondelete="SET NULL"), nullable=True),
        sa.Column("promotion_gate", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("e2e_workflow", sa.String(255), nullable=True),
        sa.Column("promote_from_env_id", sa.Integer,
                  sa.ForeignKey("environments.id", ondelete="SET NULL"), nullable=True),
        sa.Column("slug_suffix", sa.String(32), nullable=False),
        sa.Column("is_default", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("project_id", "name", name="uq_env_project_name"),
    )

    sa.Table(
        "services", m,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("project_id", sa.Integer,
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("source_ref", sa.String(512), nullable=True),
        sa.Column("is_public", sa.Boolean, nullable=False),
        sa.Column("subdomain_label", sa.String(64), nullable=False),
        sa.Column("internal_port", sa.Integer, nullable=True),
        sa.Column("depends_on", sa.JSON, nullable=False),
        sa.Column("env_template", sa.JSON, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
        sa.UniqueConstraint("project_id", "name", name="uq_service_project_name"),
    )

    sa.Table(
        "service_env_vars", m,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("service_id", sa.Integer,
                  sa.ForeignKey("services.id", ondelete="CASCADE"), index=True, nullable=False),
        sa.Column("environment_id", sa.Integer,
                  sa.ForeignKey("environments.id", ondelete="CASCADE"), nullable=True),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("is_secret", sa.Boolean, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    sa.Table(
        "deployments", m,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("environment_id", sa.Integer,
                  sa.ForeignKey("environments.id", ondelete="CASCADE"), index=True, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("stack_name", sa.String(255), nullable=False),
        sa.Column("node_id", sa.String(64), nullable=True),
        sa.Column("commit_sha", sa.String(64), nullable=True),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("log_tail", sa.Text, nullable=True),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime, index=True, nullable=False),
        sa.Column("updated_at", sa.DateTime, nullable=False),
    )

    sa.Table(
        "service_instances", m,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("deployment_id", sa.Integer,
                  sa.ForeignKey("deployments.id", ondelete="CASCADE"), index=True, nullable=False),
        sa.Column("service_id", sa.Integer,
                  sa.ForeignKey("services.id", ondelete="SET NULL"), nullable=True),
        sa.Column("service_name", sa.String(128), nullable=False),
        sa.Column("container_name", sa.String(255), nullable=True),
        sa.Column("url", sa.String(512), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    sa.Table(
        "metric_samples", m,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("service_id", sa.Integer,
                  sa.ForeignKey("services.id", ondelete="CASCADE"), index=True, nullable=False),
        sa.Column("environment_id", sa.Integer,
                  sa.ForeignKey("environments.id", ondelete="CASCADE"), index=True, nullable=True),
        sa.Column("ts", sa.DateTime, index=True, nullable=False),
        sa.Column("cpu_pct", sa.Float, nullable=False),
        sa.Column("mem_used", sa.BigInteger, nullable=False),
        sa.Column("mem_limit", sa.BigInteger, nullable=False),
        sa.Column("net_rx", sa.BigInteger, nullable=False),
        sa.Column("net_tx", sa.BigInteger, nullable=False),
    )

    sa.Table(
        "uptime_samples", m,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("component", sa.String(32), index=True, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("detail", sa.String(512), nullable=True),
        sa.Column("latency_ms", sa.Integer, nullable=True),
        sa.Column("ts", sa.DateTime, index=True, nullable=False),
    )

    sa.Table(
        "workflow_runs_cache", m,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("project_id", sa.Integer,
                  sa.ForeignKey("projects.id", ondelete="CASCADE"), index=True, nullable=True),
        sa.Column("repository_full_name", sa.String(255), index=True, nullable=False),
        sa.Column("run_id", sa.Integer, unique=True, index=True, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("status", sa.String(64), nullable=False),
        sa.Column("conclusion", sa.String(64), nullable=True),
        sa.Column("head_branch", sa.String(255), nullable=False),
        sa.Column("html_url", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime, index=True, nullable=False),
        sa.Column("fetched_at", sa.DateTime, nullable=False),
    )

    sa.Table(
        "settings", m,
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.JSON, nullable=False),
    )

    sa.Table(
        "identities", m,
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("email", sa.String(320), unique=True, index=True, nullable=False),
        sa.Column("enabled", sa.Boolean, nullable=False),
        sa.Column("last_login_at", sa.DateTime, nullable=True),
        sa.Column("last_login_provider", sa.String(16), nullable=True),
        sa.Column("login_count", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime, nullable=False),
    )

    return m


def upgrade() -> None:
    _metadata().create_all(op.get_bind())


def downgrade() -> None:
    _metadata().drop_all(op.get_bind())
