"""Store hand-picked send dates for "On specific dates" schedules."""
from alembic import op
import sqlalchemy as sa

revision = "20261018_0011"
down_revision = "20261018_0010"
branch_labels = None
depends_on = None


def upgrade():
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("jobs")}
    if "schedule_dates" not in columns:
        op.add_column("jobs", sa.Column("schedule_dates", sa.Text(), nullable=True))


def downgrade():
    with op.batch_alter_table("jobs") as batch:
        batch.drop_column("schedule_dates")
