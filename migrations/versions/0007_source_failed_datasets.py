"""per-dataset failures on source states

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-21 12:00:00
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = '0007'
down_revision = '0006'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('source_states', sa.Column('failed_datasets', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True))


def downgrade():
    op.drop_column('source_states', 'failed_datasets')
