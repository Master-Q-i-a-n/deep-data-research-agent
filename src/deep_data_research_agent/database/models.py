"""SQLAlchemy models for application-owned PostgreSQL tables."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    """Return naive UTC for portable PostgreSQL/SQLite comparisons."""

    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Declarative base for application-owned tables."""


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    username_normalized: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False
    )
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), unique=True, index=True, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class AgentThread(Base):
    __tablename__ = "agent_threads"

    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class EmailDelivery(Base):
    """Durable idempotency and audit state for an external SMTP side effect."""

    __tablename__ = "email_deliveries"

    idempotency_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    pdf_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    zip_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # Nullable keeps the in-place migration safe for historical audit rows.
    pdf_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    markdown_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    attempts: Mapped[int] = mapped_column(default=0, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_summary: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class UserTokenBucket(Base):
    """Authoritative per-user token balance; Redis only caches this state."""

    __tablename__ = "user_token_buckets"

    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    balance_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_refill_hour: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class ModelTokenUsage(Base):
    """Replay-safe model-call reservation and final provider usage."""

    __tablename__ = "model_token_usage"

    call_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    root_run_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    thread_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    agent_name: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    reserved_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    usage_source: Mapped[str] = mapped_column(String(16), default="reserved", nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


__all__ = [
    "AgentThread",
    "AuthSession",
    "Base",
    "EmailDelivery",
    "ModelTokenUsage",
    "User",
    "UserTokenBucket",
    "utcnow",
]
