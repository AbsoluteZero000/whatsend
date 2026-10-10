"""Add Relay connections and durable API submissions."""
from alembic import op
import sqlalchemy as sa

from app.models.relay_operation import RelayOperation

revision = "20261010_0004"
down_revision = "20260821_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("tokens")}
    if "provider" not in columns:
        op.add_column("tokens", sa.Column("provider", sa.String(20), nullable=False, server_default="whatsapp"))
    if "relay_session_id" not in columns:
        op.add_column("tokens", sa.Column("relay_session_id", sa.String(36), nullable=True))
    if "relay_base_url" not in columns:
        op.add_column("tokens", sa.Column("relay_base_url", sa.String(2048), nullable=True))
    RelayOperation.__table__.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    RelayOperation.__table__.drop(op.get_bind(), checkfirst=True)
    with op.batch_alter_table("tokens") as batch:
        batch.drop_column("relay_base_url")
        batch.drop_column("relay_session_id")
        batch.drop_column("provider")
