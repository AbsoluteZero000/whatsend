"""Finite message series, attachment imports, and durable occurrence claims."""
from alembic import op
import sqlalchemy as sa
from app.models.job_series import SeriesEntry, SeriesAttachment

revision = "20261014_0009"
down_revision = "20261013_0008"
branch_labels = None
depends_on = None


def upgrade():
    connection = op.get_bind()
    columns = {c['name'] for c in sa.inspect(connection).get_columns('jobs')}
    for name, column in [
        ('message_mode', sa.Column('message_mode', sa.String(10), nullable=False, server_default='repeat')),
        ('series_total', sa.Column('series_total', sa.Integer(), nullable=False, server_default='0')),
        ('series_cursor', sa.Column('series_cursor', sa.Integer(), nullable=False, server_default='0')),
        ('send_count', sa.Column('send_count', sa.Integer(), nullable=False, server_default='0')),
        ('series_plan', sa.Column('series_plan', sa.Text(), nullable=True)),
        ('series_active_run', sa.Column('series_active_run', sa.String(36), nullable=True)),
    ]:
        if name not in columns: op.add_column('jobs', column)
    columns = {c['name'] for c in sa.inspect(connection).get_columns('delivery_attempts')}
    if 'series_entry_id' not in columns:
        op.add_column('delivery_attempts', sa.Column('series_entry_id', sa.Integer(), nullable=True))
        op.create_index('ix_delivery_attempts_series_entry_id', 'delivery_attempts', ['series_entry_id'])
    if 'part_label' not in columns:
        op.add_column('delivery_attempts', sa.Column('part_label', sa.String(255), nullable=True))
    SeriesEntry.__table__.create(connection, checkfirst=True)
    SeriesAttachment.__table__.create(connection, checkfirst=True)


def downgrade():
    op.drop_table('series_attachments')
    op.drop_table('series_entries')
    with op.batch_alter_table('delivery_attempts') as batch:
        batch.drop_index('ix_delivery_attempts_series_entry_id')
        batch.drop_column('series_entry_id')
        batch.drop_column('part_label')
    with op.batch_alter_table('jobs') as batch:
        batch.drop_column('series_active_run')
        batch.drop_column('series_plan')
        batch.drop_column('send_count')
        batch.drop_column('series_cursor')
        batch.drop_column('series_total')
        batch.drop_column('message_mode')
