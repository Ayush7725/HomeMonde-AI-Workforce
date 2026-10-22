"""operations, quality, reports, providers

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('eval_results',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('run_id', sa.String(length=40), nullable=False),
    sa.Column('scenario', sa.String(length=100), nullable=False),
    sa.Column('employee', sa.String(length=64), nullable=False),
    sa.Column('strong_model', sa.String(length=150), nullable=False),
    sa.Column('cheap_model', sa.String(length=150), nullable=False),
    sa.Column('status', sa.String(length=10), nullable=False),
    sa.Column('checks', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cost_usd', sa.Float(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('eval_results', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_eval_results_run_id'), ['run_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_eval_results_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('notifications',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=30), nullable=False),
    sa.Column('severity', sa.String(length=10), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('text', sa.Text(), nullable=False),
    sa.Column('work_id', sa.String(length=40), nullable=True),
    sa.Column('employee', sa.String(length=64), nullable=True),
    sa.Column('delivered', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_notifications_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_notifications_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('reports',
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('period_start', sa.DateTime(), nullable=False),
    sa.Column('period_end', sa.DateTime(), nullable=False),
    sa.Column('local_date', sa.Date(), nullable=False),
    sa.Column('content', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('tenant_id', 'id')
    )
    op.create_table('employee_controls',
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('employee', sa.String(length=64), nullable=False),
    sa.Column('paused', sa.Boolean(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('changed_by', sa.Integer(), nullable=True),
    sa.Column('changed_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['changed_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('tenant_id', 'employee')
    )
    op.create_table('finding_reviews',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('work_id', sa.String(length=40), nullable=False),
    sa.Column('employee', sa.String(length=64), nullable=False),
    sa.Column('source', sa.String(length=10), nullable=False),
    sa.Column('verdict', sa.String(length=10), nullable=False),
    sa.Column('note', sa.Text(), nullable=False),
    sa.Column('reviewer', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['reviewer'], ['users.id'], ),
    sa.ForeignKeyConstraint(['tenant_id', 'work_id'], ['work_items.tenant_id', 'work_items.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'work_id', 'source', name='uq_review')
    )
    with op.batch_alter_table('llm_usage', schema=None) as batch_op:
        batch_op.add_column(sa.Column('task', sa.String(length=20), nullable=False, server_default='investigation'))
        batch_op.add_column(sa.Column('tier', sa.String(length=10), nullable=False, server_default='strong'))
        batch_op.add_column(sa.Column('provider', sa.String(length=40), nullable=False, server_default='anthropic'))
        batch_op.add_column(sa.Column('cost_usd', sa.Float(), nullable=True))
        batch_op.alter_column('model',
               existing_type=sa.VARCHAR(length=64),
               type_=sa.String(length=100),
               existing_nullable=False)

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('notifications_seen_at', sa.DateTime(), nullable=True))



def downgrade():
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('notifications_seen_at')

    with op.batch_alter_table('llm_usage', schema=None) as batch_op:
        batch_op.alter_column('model',
               existing_type=sa.String(length=100),
               type_=sa.VARCHAR(length=64),
               existing_nullable=False)
        batch_op.drop_column('cost_usd')
        batch_op.drop_column('provider')
        batch_op.drop_column('tier')
        batch_op.drop_column('task')

    op.drop_table('finding_reviews')
    op.drop_table('employee_controls')
    op.drop_table('reports')
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_notifications_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_notifications_created_at'))

    op.drop_table('notifications')
    with op.batch_alter_table('eval_results', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_eval_results_tenant_id'))
        batch_op.drop_index(batch_op.f('ix_eval_results_run_id'))

    op.drop_table('eval_results')
