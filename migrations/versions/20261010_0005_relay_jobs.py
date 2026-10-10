"""Persist Relay job acknowledgements and connection snapshots."""
from alembic import op
import sqlalchemy as sa

revision = "20261010_0005"
down_revision = "20261010_0004"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("delivery_attempts")}
    for name, kind in [("relay_message_id", sa.String(36)), ("relay_token_id", sa.Integer()),
                       ("relay_session_id", sa.String(36)), ("relay_base_url", sa.String(2048))]:
        if name not in columns:
            op.add_column("delivery_attempts", sa.Column(name, kind, nullable=True))


def downgrade():
    with op.batch_alter_table("delivery_attempts") as batch:
        for name in ["relay_message_id", "relay_token_id", "relay_session_id", "relay_base_url"]:
            batch.drop_column(name)
