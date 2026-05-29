"""initial schema

Captures the schema as it stood before Alembic was adopted — equivalent to
what the now-removed ALTER TABLE bootstrap in backend/database.py produced.
Pre-existing local dev databases will be stamped at this revision so the
follow-on migrations only need to add the genuinely new bits.

Revision ID: 0001_initial
Revises:
Create Date: 2026-05-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0001_initial"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("display_name", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("google_sub", sa.String(), nullable=True),
        sa.Column("subscription_tier", sa.String(), nullable=False, server_default="free"),
        sa.Column("subscription_status", sa.String(), nullable=False, server_default="active"),
        sa.Column("stripe_customer_id", sa.String(), nullable=True),
        sa.Column("stripe_subscription_id", sa.String(), nullable=True),
        sa.Column("current_period_end", sa.DateTime(), nullable=True),
        sa.Column("onboarded_at", sa.DateTime(), nullable=True),
        sa.Column("email_pref_tax_deadlines", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("email_pref_subscription", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("email_pref_weekly_digest", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_digest_sent_at", sa.DateTime(), nullable=True),
        sa.Column("last_deadline_notified", sa.String(), nullable=True, server_default=""),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("google_sub"),
    )

    op.create_table(
        "waitlist",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(), nullable=False),
        sa.Column("platforms", sa.Text(), nullable=True),
        sa.Column("monthly_volume", sa.String(), nullable=True),
        sa.Column("referrer", sa.String(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.Column("signup_date", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_waitlist_email", "waitlist", ["email"], unique=True)

    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("contact_email", sa.String(), nullable=True),
        sa.Column("matched_kb_key", sa.String(), nullable=True),
        sa.Column("needs_human", sa.Boolean(), nullable=True),
        sa.Column("answered", sa.Boolean(), nullable=True),
        sa.Column("read_by_user", sa.Boolean(), nullable=True),
        sa.Column("read_by_admin", sa.Boolean(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_chat_messages_created_at", "chat_messages", ["created_at"])
    op.create_index("ix_chat_messages_user_id", "chat_messages", ["user_id"])

    op.create_table(
        "import_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=True),
        sa.Column("created", sa.Integer(), nullable=True),
        sa.Column("skipped", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_import_logs_user_id", "import_logs", ["user_id"])

    op.create_table(
        "marketplace_connections",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("platform", sa.String(), nullable=False),
        sa.Column("account_label", sa.String(), nullable=True),
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(), nullable=True),
        sa.Column("scope", sa.Text(), nullable=True),
        sa.Column("last_sync_at", sa.DateTime(), nullable=True),
        sa.Column("last_sync_count", sa.Integer(), nullable=True),
        sa.Column("last_sync_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_marketplace_connections_user_id", "marketplace_connections", ["user_id"]
    )

    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_password_reset_tokens_token", "password_reset_tokens", ["token"], unique=True
    )
    op.create_index(
        "ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"]
    )

    op.create_table(
        "tax_settings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("tax_year", sa.Integer(), nullable=True),
        sa.Column("filing_status", sa.String(), nullable=True),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("city", sa.String(), nullable=True),
        sa.Column("ordinary_income_estimate", sa.Float(), nullable=True),
        sa.Column("classification", sa.String(), nullable=True),
        sa.Column("lot_method", sa.String(), nullable=True),
        sa.Column("prior_year_tax", sa.Float(), nullable=True),
        sa.Column("prior_year_agi", sa.Float(), nullable=True),
        sa.Column("withholding_paid", sa.Float(), nullable=True),
        sa.Column("is_kiddie_filer", sa.Boolean(), nullable=True),
        sa.Column("parent_marginal_rate", sa.Float(), nullable=True),
        sa.Column("nol_carryforward", sa.Float(), nullable=True),
        sa.Column("quiz_answers", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id"),
    )

    op.create_table(
        "transactions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("item_title", sa.String(), nullable=False),
        sa.Column("sale_date", sa.Date(), nullable=False),
        sa.Column("sale_price", sa.Float(), nullable=False),
        sa.Column("platform_fees", sa.Float(), nullable=False),
        sa.Column("shipping_charged", sa.Float(), nullable=False),
        sa.Column("shipping_cost_out", sa.Float(), nullable=False),
        sa.Column("purchase_date", sa.Date(), nullable=True),
        sa.Column("purchase_price", sa.Float(), nullable=False),
        sa.Column("grading_fees", sa.Float(), nullable=False),
        sa.Column("other_basis_costs", sa.Float(), nullable=False),
        sa.Column("platform_source", sa.String(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("image_path", sa.String(), nullable=True),
        sa.Column("card_category", sa.String(), nullable=True),
        sa.Column("acquisition_type", sa.String(), nullable=True),
        sa.Column("donor_basis", sa.Float(), nullable=True),
        sa.Column("gift_date", sa.Date(), nullable=True),
        sa.Column("fmv_at_gift", sa.Float(), nullable=True),
        sa.Column("donor_holding_period_start", sa.Date(), nullable=True),
        sa.Column("date_of_death", sa.Date(), nullable=True),
        sa.Column("fmv_at_death", sa.Float(), nullable=True),
        sa.Column("break_spot_price", sa.Float(), nullable=True),
        sa.Column("break_total_fmv", sa.Float(), nullable=True),
        sa.Column("donation_date", sa.Date(), nullable=True),
        sa.Column("fmv_at_donation", sa.Float(), nullable=True),
        sa.Column("donee_organization", sa.String(), nullable=True),
        sa.Column("donee_unrelated_use", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "cost_basis_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("transaction_id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("note", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["transaction_id"], ["transactions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("cost_basis_items")
    op.drop_table("transactions")
    op.drop_table("tax_settings")
    op.drop_index("ix_password_reset_tokens_user_id", table_name="password_reset_tokens")
    op.drop_index("ix_password_reset_tokens_token", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
    op.drop_index(
        "ix_marketplace_connections_user_id", table_name="marketplace_connections"
    )
    op.drop_table("marketplace_connections")
    op.drop_index("ix_import_logs_user_id", table_name="import_logs")
    op.drop_table("import_logs")
    op.drop_index("ix_chat_messages_user_id", table_name="chat_messages")
    op.drop_index("ix_chat_messages_created_at", table_name="chat_messages")
    op.drop_table("chat_messages")
    op.drop_index("ix_waitlist_email", table_name="waitlist")
    op.drop_table("waitlist")
    op.drop_table("users")
