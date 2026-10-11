"""Preserve the original attachment filename for scheduled deliveries."""
from alembic import op
import sqlalchemy as sa

revision = "20261011_0007"
down_revision = "20261011_0006"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("jobs")}
    if "image_filename" not in columns:
        op.add_column("jobs", sa.Column("image_filename", sa.String(255), nullable=True))


def downgrade():
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("image_filename")
