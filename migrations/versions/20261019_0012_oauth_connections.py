"""Add OAuth clients and grants for the Claude connector."""
from alembic import op
import sqlalchemy as sa

revision = "20261019_0012"
down_revision = "20261018_0011"
branch_labels = None
depends_on = None


def upgrade():
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "oauth_clients" not in tables:
        op.create_table(
            "oauth_clients",
            sa.Column("id", sa.String(64), primary_key=True),
            sa.Column("name", sa.String(100), nullable=False),
            sa.Column("redirect_uris", sa.Text(), nullable=False),
            sa.Column("auth_method", sa.String(30), nullable=False, server_default="none"),
            sa.Column("secret_hash", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        )
    if "oauth_grants" not in tables:
        op.create_table(
            "oauth_grants",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("client_id", sa.String(64), sa.ForeignKey("oauth_clients.id", ondelete="CASCADE"), nullable=False),
            sa.Column("client_name", sa.String(100), nullable=False),
            sa.Column("scope", sa.String(100), nullable=False, server_default="messages"),
            sa.Column("resource", sa.String(512), nullable=True),
            sa.Column("redirect_uri", sa.String(2048), nullable=True),
            sa.Column("code_hash", sa.String(64), nullable=True, unique=True),
            sa.Column("code_challenge", sa.String(128), nullable=True),
            sa.Column("code_expires_at", sa.DateTime(), nullable=True),
            sa.Column("access_hash", sa.String(64), nullable=True, unique=True),
            sa.Column("access_expires_at", sa.DateTime(), nullable=True),
            sa.Column("refresh_hash", sa.String(64), nullable=True, unique=True),
            sa.Column("refresh_expires_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
        )
        op.create_index("ix_oauth_grants_user_id", "oauth_grants", ["user_id"])


def downgrade():
    op.drop_index("ix_oauth_grants_user_id", table_name="oauth_grants")
    op.drop_table("oauth_grants")
    op.drop_table("oauth_clients")
