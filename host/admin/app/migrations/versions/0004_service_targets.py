"""per-service+environment deployment targets

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-14

service_targets: where one service of one environment deploys — the homebox
default (no row / target='homebox') or a connected cloud account
(aws/gcp/cloudflare Integration). environment_id NULL = the service-wide
default row (ServiceEnvVar convention). Split timestamps: `updated_at` covers
the user-intent columns, `state_updated_at` the coordinator-written machine
state — cluster sync resolves each group newer-wins independently.

service_instances.target records where each deployed instance runs so the UI
and verify paths can tell a local container from a cloud endpoint.
"""
import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "service_targets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("service_id", sa.Integer(),
                  sa.ForeignKey("services.id", ondelete="CASCADE"),
                  nullable=False, index=True),
        sa.Column("environment_id", sa.Integer(),
                  sa.ForeignKey("environments.id", ondelete="CASCADE"),
                  nullable=True),
        sa.Column("target", sa.String(16), nullable=False, server_default="homebox"),
        sa.Column("integration_id", sa.Integer(),
                  sa.ForeignKey("integrations.id", ondelete="SET NULL"),
                  nullable=True),
        sa.Column("config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("state", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("state_updated_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "service_instances",
        sa.Column("target", sa.String(16), nullable=False, server_default="homebox"),
    )


def downgrade() -> None:
    op.drop_column("service_instances", "target")
    op.drop_table("service_targets")
