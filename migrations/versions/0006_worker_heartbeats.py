"""worker heartbeats

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-20 12:30:00
"""
from alembic import op
import sqlalchemy as sa


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('heartbeats',
                    sa.Column('name', sa.String(length=100), nullable=False),
                    sa.Column('beat_at', sa.DateTime(), nullable=False),
                    sa.Column('started_at', sa.DateTime(), nullable=False),
                    sa.PrimaryKeyConstraint('name'))


def downgrade():
    op.drop_table('heartbeats')
