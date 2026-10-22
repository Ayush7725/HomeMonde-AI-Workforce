"""controls, access grants, drill-down, stop and assign

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('access_grants',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('areas', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('granted_by', sa.Integer(), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['granted_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('access_grants', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_access_grants_tenant_id'), ['tenant_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_access_grants_user_id'), ['user_id'], unique=False)

    op.create_table('controls',
    sa.Column('tenant_id', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=10), nullable=False),
    sa.Column('target', sa.String(length=64), nullable=False),
    sa.Column('paused', sa.Boolean(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('changed_by', sa.Integer(), nullable=True),
    sa.Column('changed_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['changed_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('tenant_id', 'kind', 'target')
    )
    # keep any existing employee pauses
    op.execute("INSERT INTO controls (tenant_id, kind, target, paused, reason, changed_by, changed_at) "
               "SELECT tenant_id, 'employee', employee, paused, reason, changed_by, changed_at FROM employee_controls")
    op.drop_table('employee_controls')
    with op.batch_alter_table('approvals', schema=None) as batch_op:
        batch_op.add_column(sa.Column('assigned_to', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_approvals_assigned_to', 'users', ['assigned_to'], ['id'])

    with op.batch_alter_table('evidence', schema=None) as batch_op:
        batch_op.add_column(sa.Column('record_ids', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=False, server_default='[]'))
        batch_op.add_column(sa.Column('record_count', sa.Integer(), nullable=False, server_default='0'))

    with op.batch_alter_table('records', schema=None) as batch_op:
        batch_op.add_column(sa.Column('raw', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True))

    with op.batch_alter_table('work_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('stop_requested', sa.Boolean(), nullable=False, server_default=sa.false()))



def downgrade():
    with op.batch_alter_table('work_items', schema=None) as batch_op:
        batch_op.drop_column('stop_requested')

    with op.batch_alter_table('records', schema=None) as batch_op:
        batch_op.drop_column('raw')

    with op.batch_alter_table('evidence', schema=None) as batch_op:
        batch_op.drop_column('record_count')
        batch_op.drop_column('record_ids')

    with op.batch_alter_table('approvals', schema=None) as batch_op:
        # WARNING: constraint name is None; this directive will fail as
        # rendered.  Add a name, or use a naming convention; see
        # https://alembic.sqlalchemy.org/en/latest/naming.html
        batch_op.drop_constraint('fk_approvals_assigned_to', type_='foreignkey')
        batch_op.drop_column('assigned_to')

    op.create_table('employee_controls',
    sa.Column('tenant_id', sa.VARCHAR(length=64), nullable=False),
    sa.Column('employee', sa.VARCHAR(length=64), nullable=False),
    sa.Column('paused', sa.BOOLEAN(), nullable=False),
    sa.Column('reason', sa.TEXT(), nullable=False),
    sa.Column('changed_by', sa.INTEGER(), nullable=True),
    sa.Column('changed_at', sa.DATETIME(), nullable=False),
    sa.ForeignKeyConstraint(['changed_by'], ['users.id'], ),
    sa.ForeignKeyConstraint(['tenant_id'], ['tenants.id'], ),
    sa.PrimaryKeyConstraint('tenant_id', 'employee')
    )
    op.drop_table('controls')
    with op.batch_alter_table('access_grants', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_access_grants_user_id'))
        batch_op.drop_index(batch_op.f('ix_access_grants_tenant_id'))

    op.drop_table('access_grants')
