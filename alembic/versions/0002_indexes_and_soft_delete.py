"""indexes and soft delete

Adds:
- ``transactions.deleted_at`` (nullable timestamp) for soft-delete semantics
- Indexes on the hot-path columns:
    transactions.user_id
    transactions.created_at
    transactions.platform_source
    transactions.deleted_at
    cost_basis_items.transaction_id

The other per-user / per-token indexes were already present in the baseline.

Revision ID: 0002_indexes_and_soft_delete
Revises: 0001_initial
Create Date: 2026-05-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_indexes_and_soft_delete"
down_revision: Union[str, Sequence[str], None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.add_column(sa.Column("deleted_at", sa.DateTime(), nullable=True))

    op.create_index(
        "ix_transactions_user_id", "transactions", ["user_id"]
    )
    op.create_index(
        "ix_transactions_created_at", "transactions", ["created_at"]
    )
    op.create_index(
        "ix_transactions_platform_source", "transactions", ["platform_source"]
    )
    op.create_index(
        "ix_transactions_deleted_at", "transactions", ["deleted_at"]
    )
    op.create_index(
        "ix_cost_basis_items_transaction_id", "cost_basis_items", ["transaction_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cost_basis_items_transaction_id", table_name="cost_basis_items"
    )
    op.drop_index("ix_transactions_deleted_at", table_name="transactions")
    op.drop_index("ix_transactions_platform_source", table_name="transactions")
    op.drop_index("ix_transactions_created_at", table_name="transactions")
    op.drop_index("ix_transactions_user_id", table_name="transactions")
    with op.batch_alter_table("transactions") as batch_op:
        batch_op.drop_column("deleted_at")
