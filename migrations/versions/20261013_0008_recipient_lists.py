"""Save account- and connection-scoped recipient lists."""
from alembic import op
import sqlalchemy as sa

revision = "20261013_0008"
down_revision = "20261011_0007"
branch_labels = None
depends_on = None


def upgrade():
    # The baseline migration creates current metadata on fresh installations.
    if "recipient_lists" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "recipient_lists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("token_id", sa.Integer(), sa.ForeignKey("tokens.id", ondelete="CASCADE"), nullable=False),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("recipients", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_recipient_lists_user_id", "recipient_lists", ["user_id"])
    op.create_index("ix_recipient_lists_token_id", "recipient_lists", ["token_id"])


def downgrade():
    op.drop_table("recipient_lists")
