"""Baseline existing installations and add reliability fields.

Revision ID: 20260814_0001
Revises:
Create Date: 2026-08-14
"""
from alembic import op
import sqlalchemy as sa

from app.database import Base
from app import models  # noqa: F401

revision = "20260814_0001"
down_revision = None
branch_labels = None
depends_on = None


def _column_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table)}


def _index_names(inspector: sa.Inspector, table: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind)
    inspector = sa.inspect(bind)

    job_columns = _column_names(inspector, "jobs")
    additions = [
        ("group_name", sa.Column("group_name", sa.String(255), nullable=True)),
        ("skip_count", sa.Column("skip_count", sa.Integer(), server_default="0", nullable=False)),
        ("schedule_timezone", sa.Column("schedule_timezone", sa.String(64), server_default="UTC", nullable=False)),
        ("retry_count", sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False)),
        ("retry_at", sa.Column("retry_at", sa.String(32), nullable=True)),
        ("retry_group_ids", sa.Column("retry_group_ids", sa.Text(), nullable=True)),
    ]
    for name, column in additions:
        if name not in job_columns:
            op.add_column("jobs", column)

    user_columns = _column_names(inspector, "users")
    if "timezone" not in user_columns:
        op.add_column("users", sa.Column("timezone", sa.String(64), server_default="UTC", nullable=False))
    if "lang" not in user_columns:
        op.add_column("users", sa.Column("lang", sa.String(2), server_default="en", nullable=False))
    if "onboarded" not in user_columns:
        op.add_column("users", sa.Column("onboarded", sa.Boolean(), server_default=sa.false(), nullable=False))

    api_key_columns = _column_names(inspector, "api_keys")
    if "token_id" not in api_key_columns:
        op.add_column("api_keys", sa.Column("token_id", sa.Integer(), sa.ForeignKey("tokens.id"), nullable=True))

    inspector = sa.inspect(bind)
    for table, index_name, columns in (
        ("jobs", "ix_jobs_user_status", ["user_id", "status"]),
        ("logs", "ix_logs_job_sent_at", ["job_id", "sent_at"]),
        ("tokens", "ix_tokens_user_active", ["user_id", "is_active"]),
    ):
        if index_name not in _index_names(inspector, table):
            op.create_index(index_name, table, columns)


def downgrade() -> None:
    for table, index_name in (
        ("jobs", "ix_jobs_user_status"),
        ("logs", "ix_logs_job_sent_at"),
        ("tokens", "ix_tokens_user_active"),
    ):
        inspector = sa.inspect(op.get_bind())
        if index_name in _index_names(inspector, table):
            op.drop_index(index_name, table_name=table)
    inspector = sa.inspect(op.get_bind())
    if "delivery_attempts" in inspector.get_table_names():
        op.drop_table("delivery_attempts")
    job_columns = _column_names(inspector, "jobs")
    with op.batch_alter_table("jobs") as batch:
        for column in ("retry_group_ids", "retry_at", "retry_count", "schedule_timezone"):
            if column in job_columns:
                batch.drop_column(column)
