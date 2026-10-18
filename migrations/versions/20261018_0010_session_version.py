"""Track a per-user session version so password changes and "sign out everywhere" end old sessions."""
from alembic import op
import sqlalchemy as sa

revision = "20261018_0010"
down_revision = "20261014_0009"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("users")}
    if "session_version" not in columns:
        op.add_column("users", sa.Column("session_version", sa.Integer(), nullable=False, server_default="0"))


def downgrade():
    with op.batch_alter_table("users") as batch:
        batch.drop_column("session_version")
