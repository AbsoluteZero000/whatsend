"""Replace external Relay connections with built-in WhatsApp connections."""
from alembic import op
import sqlalchemy as sa
from uuid import uuid4

revision = "20261011_0006"
down_revision = "20261010_0005"
branch_labels = None
depends_on = None


def upgrade():
    # Keep job/key links and delivery history; users pair the new built-in session.
    for row in op.get_bind().execute(sa.text("SELECT id, relay_session_id FROM tokens WHERE provider='relay'")):
        op.get_bind().execute(sa.text("UPDATE tokens SET provider='native', relay_base_url=NULL, api_token='', relay_session_id=:session WHERE id=:id"),
                              {"session": str(uuid4()), "id": row.id})
    op.execute(sa.text("UPDATE relay_operations SET status='unknown', response='{}' WHERE status IN ('submitting','queued','processing')"))
    op.execute(sa.text("UPDATE delivery_attempts SET status='unknown', response='External Relay retired. Check old delivery history before resending.' WHERE relay_token_id IS NOT NULL AND status IN ('submitting','queued','processing')"))
    op.execute(sa.text("UPDATE jobs SET status='unknown' WHERE status IN ('queued','processing') AND token_id IN (SELECT id FROM tokens WHERE provider='native')"))


def downgrade():
    # Pairing credentials cannot be converted back into external Relay credentials.
    pass
