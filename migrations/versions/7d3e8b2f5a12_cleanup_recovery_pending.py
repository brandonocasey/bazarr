"""Track unfinished cleanup recovery independently of operation status."""

from alembic import op
import sqlalchemy as sa

revision = '7d3e8b2f5a12'
down_revision = '6c2d7a9e4b11'
branch_labels = None
depends_on = None


def upgrade():
    columns = sa.inspect(op.get_bind()).get_columns('table_subtitle_cleanup_operations')
    if any(column['name'] == 'recovery_pending' for column in columns):
        return
    op.add_column('table_subtitle_cleanup_operations', sa.Column(
        'recovery_pending', sa.Boolean(), nullable=False, server_default=sa.false()))
    operations = sa.table('table_subtitle_cleanup_operations',
                          sa.column('status', sa.Text()), sa.column('recovery_pending', sa.Boolean()))
    op.execute(operations.update().values(
        recovery_pending=operations.c.status.in_(('applying', 'restoring', 'failed'))))
