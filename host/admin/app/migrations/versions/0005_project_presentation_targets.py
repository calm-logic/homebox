"""project icons and project-level deployment targets

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-18
"""
import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("icon", sa.Text(), nullable=True))
    op.add_column(
        "projects",
        sa.Column("deployment_target", sa.String(16), nullable=False,
                  server_default="homebox"),
    )
    op.add_column(
        "projects",
        sa.Column("deployment_target_integration_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_projects_deployment_target_integration",
        "projects", "integrations",
        ["deployment_target_integration_id"], ["id"],
        ondelete="SET NULL",
    )
    op.add_column(
        "projects",
        sa.Column("deployment_target_config", sa.JSON(), nullable=False,
                  server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("projects", "deployment_target_config")
    op.drop_constraint(
        "fk_projects_deployment_target_integration", "projects", type_="foreignkey")
    op.drop_column("projects", "deployment_target_integration_id")
    op.drop_column("projects", "deployment_target")
    op.drop_column("projects", "icon")
