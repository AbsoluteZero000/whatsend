"""Add the administrator role to users.

Revision ID: 20260821_0002
Revises: 20260814_0001
Create Date: 2026-08-21
"""
from alembic import op
import sqlalchemy as sa


revision = "20260821_0002"
down_revision = "20260814_0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "is_admin" not in columns:
        op.add_column(
            "users",
            sa.Column("is_admin", sa.Boolean(), server_default=sa.false(), nullable=False),
        )


def downgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("users")}
    if "is_admin" in columns:
        with op.batch_alter_table("users") as batch:
            batch.drop_column("is_admin")
