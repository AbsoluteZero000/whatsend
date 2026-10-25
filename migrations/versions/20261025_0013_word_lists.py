"""Store changing-word lists: the next item replaces {word} in the message at each send."""
from alembic import op
import sqlalchemy as sa

revision = "20261025_0013"
down_revision = "20261019_0012"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("jobs")}
    if "word_list" not in columns:
        op.add_column("jobs", sa.Column("word_list", sa.Text(), nullable=True))
    if "word_list_next" not in columns:
        op.add_column("jobs", sa.Column("word_list_next", sa.Integer(), nullable=False, server_default="0"))
    if "word_list_end" not in columns:
        op.add_column("jobs", sa.Column("word_list_end", sa.String(10), nullable=False, server_default="repeat"))


def downgrade():
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("word_list_end")
        batch.drop_column("word_list_next")
        batch.drop_column("word_list")
