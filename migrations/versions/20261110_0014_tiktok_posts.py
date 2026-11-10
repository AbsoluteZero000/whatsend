"""Durable TikTok publishing records."""
from alembic import op
from app.providers.tiktok.models import TikTokPost

revision = '20261110_0014'
down_revision = '20261025_0013'
branch_labels = None
depends_on = None


def upgrade():
    TikTokPost.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    op.drop_table('tiktok_posts')
