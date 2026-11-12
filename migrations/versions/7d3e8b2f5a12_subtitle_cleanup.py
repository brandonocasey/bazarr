"""Add durable subtitle cleanup operation records."""

from alembic import op
import sqlalchemy as sa

revision = '7d3e8b2f5a12'
down_revision = '3b8e5d1a7f20'
branch_labels = None
depends_on = None


def upgrade():
    if 'table_subtitle_cleanup_operations' in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        'table_subtitle_cleanup_operations',
        sa.Column('id', sa.Text(), primary_key=True),
        sa.Column('media_type', sa.Text(), nullable=False),
        sa.Column('media_id', sa.Integer(), nullable=False),
        sa.Column('video_path', sa.Text(), nullable=False),
        sa.Column('status', sa.Text(), nullable=False),
        sa.Column('job_id', sa.Integer(), nullable=True),
        sa.Column('recovery_pending', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_table_subtitle_cleanup_operations_video_path',
                    'table_subtitle_cleanup_operations', ['video_path'])
