"""Baseline the current application-owned schema.

Revision ID: 0001_current_schema
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0001_current_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=32), nullable=False),
        sa.Column("username_normalized", sa.String(length=32), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("is_system", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_username_normalized", "users", ["username_normalized"], unique=True)

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_index("ix_auth_sessions_token_hash", "auth_sessions", ["token_hash"], unique=True)
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])

    op.create_table(
        "agent_threads",
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("thread_id"),
    )
    op.create_index("ix_agent_threads_user_id", "agent_threads", ["user_id"])

    op.create_table(
        "email_deliveries",
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("thread_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("pdf_filename", sa.String(length=255), nullable=False),
        sa.Column("zip_filename", sa.String(length=255), nullable=False),
        sa.Column("pdf_path", sa.String(length=1024), nullable=True),
        sa.Column("markdown_path", sa.String(length=1024), nullable=True),
        sa.Column("message_id", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(), nullable=False),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("error_summary", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("idempotency_key"),
    )
    op.create_index("ix_email_deliveries_status", "email_deliveries", ["status"])
    op.create_index("ix_email_deliveries_thread_id", "email_deliveries", ["thread_id"])
    op.create_index("ix_email_deliveries_user_id", "email_deliveries", ["user_id"])

    op.create_table(
        "user_token_buckets",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("balance_tokens", sa.BigInteger(), nullable=False),
        sa.Column("last_refill_hour", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "model_token_usage",
        sa.Column("call_id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("root_run_id", sa.String(length=64), nullable=True),
        sa.Column("thread_id", sa.String(length=64), nullable=True),
        sa.Column("agent_name", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=128), nullable=False),
        sa.Column("reserved_tokens", sa.BigInteger(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), nullable=True),
        sa.Column("output_tokens", sa.BigInteger(), nullable=True),
        sa.Column("total_tokens", sa.BigInteger(), nullable=True),
        sa.Column("usage_source", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("settled_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("call_id"),
    )
    op.create_index("ix_model_token_usage_root_run_id", "model_token_usage", ["root_run_id"])
    op.create_index("ix_model_token_usage_status", "model_token_usage", ["status"])
    op.create_index("ix_model_token_usage_thread_id", "model_token_usage", ["thread_id"])
    op.create_index("ix_model_token_usage_user_id", "model_token_usage", ["user_id"])


def downgrade() -> None:
    op.drop_table("model_token_usage")
    op.drop_table("user_token_buckets")
    op.drop_table("email_deliveries")
    op.drop_table("agent_threads")
    op.drop_table("auth_sessions")
    op.drop_table("users")
