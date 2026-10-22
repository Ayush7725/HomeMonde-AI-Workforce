"""indexes for queue, decision, audit, usage and notification lookups

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-20 12:00:00
"""
from alembic import op


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

INDEXES = [
    ("ix_work_tenant_status", "work_items", ["tenant_id", "status"]),
    ("ix_work_tenant_rule", "work_items", ["tenant_id", "rule_id"]),
    ("ix_approval_tenant_status", "approvals", ["tenant_id", "status"]),
    ("ix_approval_tenant_work", "approvals", ["tenant_id", "work_id"]),
    ("ix_event_tenant_type_ts", "events", ["tenant_id", "type", "ts"]),
    ("ix_usage_tenant_created", "llm_usage", ["tenant_id", "created_at"]),
    ("ix_usage_tenant_work", "llm_usage", ["tenant_id", "work_id"]),
    ("ix_notification_tenant_delivered", "notifications", ["tenant_id", "delivered"]),
]


def upgrade():
    for name, table, cols in INDEXES:
        op.create_index(name, table, cols)


def downgrade():
    for name, table, _ in reversed(INDEXES):
        op.drop_index(name, table_name=table)
