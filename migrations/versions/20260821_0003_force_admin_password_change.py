"""Track required password changes for bootstrap accounts.

Revision ID: 20260821_0003
Revises: 20260821_0002
Create Date: 2026-08-21
"""
from alembic import op
import sqlalchemy as sa


revision = "20260821_0003"
down_revision = "20260821_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "must_change_password" not in columns:
        op.add_column(
            "users",
            sa.Column("must_change_password", sa.Boolean(), server_default=sa.false(), nullable=False),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "must_change_password" in columns:
        with op.batch_alter_table("users") as batch:
            batch.drop_column("must_change_password")
