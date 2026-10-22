"""initial schema

Revision ID: 0001
Revises: 
Create Date: 2026-10-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0001'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('tenants',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('config_hash', sa.String(length=64), nullable=False),
    sa.Column('event_seq', sa.Integer(), nullable=False),
    sa.Column('last_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('seq', sa.Integer(), nullable=False),
    sa.Column('ts', sa.DateTime(), nullable=False),
    sa.Column('type', sa.String(length=40), nullable=False),
    sa.Column('category', sa.String(length=16), nullable=False),
    sa.Column('actor', sa.String(length=100), nullable=False),
    sa.Column('work_id', sa.String(length=40), nullable=True),
    sa.Column('msg', sa.Text(), nullable=False),
    sa.Column('payload', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('prev_hash', sa.String(length=64), nullable=False),
    sa.Column('hash', sa.String(length=64), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'seq', name='uq_event_seq')
    )
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_events_work_id'), ['work_id'], unique=False)

    op.create_table('id_counters',
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('prefix', sa.String(length=16), nullable=False),
    sa.Column('value', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('tenant_id', 'prefix')
    )
    op.create_table('llm_usage',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('work_id', sa.String(length=40), nullable=True),
    sa.Column('employee', sa.String(length=64), nullable=False),
    sa.Column('model', sa.String(length=64), nullable=False),
    sa.Column('input_tokens', sa.Integer(), nullable=False),
    sa.Column('output_tokens', sa.Integer(), nullable=False),
    sa.Column('cache_read_tokens', sa.Integer(), nullable=False),
    sa.Column('cache_write_tokens', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('llm_usage', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_llm_usage_created_at'), ['created_at'], unique=False)

    op.create_table('records',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('dataset', sa.String(length=64), nullable=False),
    sa.Column('key', sa.String(length=300), nullable=False),
    sa.Column('occurred_on', sa.Date(), nullable=True),
    sa.Column('data', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('synced_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'dataset', 'key', name='uq_record_key')
    )
    with op.batch_alter_table('records', schema=None) as batch_op:
        batch_op.create_index('ix_record_window', ['tenant_id', 'dataset', 'occurred_on'], unique=False)

    op.create_table('source_states',
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('source_id', sa.String(length=64), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('last_attempt', sa.DateTime(), nullable=True),
    sa.Column('last_success', sa.DateTime(), nullable=True),
    sa.Column('error', sa.Text(), nullable=False),
    sa.Column('rows', sa.Integer(), nullable=False),
    sa.Column('rejected', sa.Integer(), nullable=False),
    sa.Column('consecutive_failures', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('tenant_id', 'source_id')
    )
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('email', sa.String(length=254), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False),
    sa.Column('areas', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('approval_limit', sa.Float(), nullable=True),
    sa.Column('password_hash', sa.String(length=300), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('failed_logins', sa.Integer(), nullable=False),
    sa.Column('locked_until', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_users_tenant_id'), ['tenant_id'], unique=False)

    op.create_table('sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('csrf_token', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('ip', sa.String(length=64), nullable=False),
    sa.Column('user_agent', sa.String(length=300), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sessions_user_id'), ['user_id'], unique=False)

    op.create_table('work_items',
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('type', sa.String(length=64), nullable=False),
    sa.Column('priority', sa.String(length=10), nullable=False),
    sa.Column('employee', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('subject', sa.String(length=500), nullable=False),
    sa.Column('context', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('origin', sa.String(length=20), nullable=False),
    sa.Column('rule_id', sa.String(length=64), nullable=True),
    sa.Column('dedupe_key', sa.String(length=300), nullable=True),
    sa.Column('parent_id', sa.String(length=40), nullable=True),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('next_step', sa.String(length=300), nullable=False),
    sa.Column('outcome', sa.String(length=40), nullable=True),
    sa.Column('finding', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('next_attempt_at', sa.DateTime(), nullable=True),
    sa.Column('lease_until', sa.DateTime(), nullable=True),
    sa.Column('error', sa.Text(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.Column('completed_at', sa.DateTime(), nullable=True),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['tenant_id', 'parent_id'], ['work_items.tenant_id', 'work_items.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('tenant_id', 'id')
    )
    with op.batch_alter_table('work_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_work_items_dedupe_key'), ['dedupe_key'], unique=False)
        batch_op.create_index(batch_op.f('ix_work_items_status'), ['status'], unique=False)

    op.create_table('approvals',
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('work_id', sa.String(length=40), nullable=False),
    sa.Column('employee', sa.String(length=64), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('params', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('amount', sa.Float(), nullable=True),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('rationale', sa.Text(), nullable=False),
    sa.Column('status', sa.String(length=30), nullable=False),
    sa.Column('round', sa.Integer(), nullable=False),
    sa.Column('decided_by', sa.Integer(), nullable=True),
    sa.Column('decided_at', sa.DateTime(), nullable=True),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('final_params', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True),
    sa.Column('requested_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['decided_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['tenant_id', 'work_id'], ['work_items.tenant_id', 'work_items.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('tenant_id', 'id')
    )
    op.create_table('evidence',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('work_id', sa.String(length=40), nullable=False),
    sa.Column('ref', sa.String(length=16), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('employee', sa.String(length=64), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('args', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('result', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('freshness', sa.String(length=16), nullable=False),
    sa.Column('source', sa.String(length=200), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id', 'work_id'], ['work_items.tenant_id', 'work_items.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'work_id', 'ref', name='uq_evidence_ref')
    )
    with op.batch_alter_table('evidence', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_evidence_work_id'), ['work_id'], unique=False)

    op.create_table('actions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('idempotency_key', sa.String(length=100), nullable=False),
    sa.Column('approval_id', sa.String(length=40), nullable=True),
    sa.Column('work_id', sa.String(length=40), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('params', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('external_ref', sa.String(length=200), nullable=False),
    sa.Column('detail', sa.Text(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['tenant_id', 'approval_id'], ['approvals.tenant_id', 'approvals.id'], ),
    sa.ForeignKeyConstraint(['tenant_id', 'work_id'], ['work_items.tenant_id', 'work_items.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('idempotency_key')
    )
    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_actions_tenant_id'), ['tenant_id'], unique=False)



def downgrade():
    with op.batch_alter_table('actions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_actions_tenant_id'))

    op.drop_table('actions')
    with op.batch_alter_table('evidence', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_evidence_work_id'))

    op.drop_table('evidence')
    op.drop_table('approvals')
    with op.batch_alter_table('work_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_work_items_status'))
        batch_op.drop_index(batch_op.f('ix_work_items_dedupe_key'))

    op.drop_table('work_items')
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sessions_user_id'))

    op.drop_table('sessions')
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_users_tenant_id'))

    op.drop_table('users')
    op.drop_table('source_states')
    with op.batch_alter_table('records', schema=None) as batch_op:
        batch_op.drop_index('ix_record_window')

    op.drop_table('records')
    with op.batch_alter_table('llm_usage', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_llm_usage_created_at'))

    op.drop_table('llm_usage')
    op.drop_table('id_counters')
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_events_work_id'))

    op.drop_table('events')
    op.drop_table('tenants')
