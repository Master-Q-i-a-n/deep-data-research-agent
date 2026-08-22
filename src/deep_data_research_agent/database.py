"""PostgreSQL persistence for accounts, sessions, ownership, and deliveries."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import math
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, delete, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from deep_data_research_agent.config import get_settings

DEFAULT_USER_ID = "local-user"
DEFAULT_USERNAME = "default"
logger = logging.getLogger(__name__)
_DEVELOPMENT_RATE_LIMIT_SECRET = secrets.token_bytes(32)
_development_secret_warning_emitted = False


def _utcnow() -> datetime:
    """Return naive UTC for portable PostgreSQL/SQLite test comparisons."""

    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    """Declarative base for the small authentication schema."""


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(32), nullable=False)
    username_normalized: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False
    )
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )


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
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )


class AgentThread(Base):
    __tablename__ = "agent_threads"

    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )


class RateLimitBucket(Base):
    """One fixed-window counter keyed by a non-reversible request identity."""

    __tablename__ = "rate_limit_buckets"

    scope: Mapped[str] = mapped_column(String(32), primary_key=True)
    key_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)


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
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    error_summary: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, nullable=False
    )


@dataclass(frozen=True, slots=True)
class UserRecord:
    """User information safe to return to authentication callers."""

    id: str
    username: str
    is_system: bool
    password_hash: str | None = None


@dataclass(frozen=True, slots=True)
class EmailDeliveryRecord:
    """Immutable delivery state returned to the report-email tool."""

    idempotency_key: str
    thread_id: str
    user_id: str
    recipient: str
    subject: str
    pdf_filename: str
    zip_filename: str
    message_id: str
    status: str
    error_summary: str | None


@dataclass(frozen=True, slots=True)
class RateLimitDecision:
    """Result of atomically consuming one fixed-window allowance."""

    allowed: bool
    count: int
    limit: int
    retry_after_seconds: int


class UsernameExistsError(ValueError):
    """Raised when a normalized username is already registered."""


class ThreadOwnershipError(PermissionError):
    """Raised when a thread is already assigned to another user."""


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
_init_lock = asyncio.Lock()
_initialized = False


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _rate_limit_secret() -> bytes:
    """Return a stable production secret or an ephemeral development secret."""

    global _development_secret_warning_emitted
    configured = get_settings().rate_limit_key_secret.get_secret_value()
    if configured:
        return configured.encode("utf-8")
    if not _development_secret_warning_emitted:
        logger.warning(
            "RATE_LIMIT_KEY_SECRET 未配置；开发环境限流键将在进程重启后变化"
        )
        _development_secret_warning_emitted = True
    return _DEVELOPMENT_RATE_LIMIT_SECRET


def _rate_limit_key_hash(scope: str, raw_key: str) -> str:
    """Hash low-entropy IP and username keys before persistence."""

    payload = f"{scope}\0{raw_key}".encode()
    return hmac.new(_rate_limit_secret(), payload, hashlib.sha256).hexdigest()


def _rate_limit_window(
    now: datetime,
    window_seconds: int,
) -> tuple[datetime, datetime]:
    """Return deterministic UTC fixed-window boundaries."""

    aware = now.replace(tzinfo=UTC) if now.tzinfo is None else now.astimezone(UTC)
    epoch = int(aware.timestamp())
    start_epoch = epoch - (epoch % window_seconds)
    start = datetime.fromtimestamp(start_epoch, UTC).replace(tzinfo=None)
    return start, start + timedelta(seconds=window_seconds)


def get_engine() -> AsyncEngine:
    """Create the process-scoped async engine only when persistence is used."""

    global _engine, _session_factory
    if _engine is None:
        settings = get_settings()
        uri = settings.postgres_uri.strip()
        if not uri:
            raise RuntimeError("POSTGRES_URI 未配置，无法使用账户与用户检查点")
        _engine = create_async_engine(
            sqlalchemy_postgres_uri(uri),
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=settings.postgres_app_pool_size,
            max_overflow=settings.postgres_app_max_overflow,
            pool_timeout=settings.postgres_pool_timeout_seconds,
        )
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def _postgres_url(uri: str) -> URL:
    """Validate one PostgreSQL URL before selecting a concrete driver."""

    try:
        url = make_url(uri)
    except Exception as exc:  # SQLAlchemy raises several URL parse subclasses.
        raise ValueError("POSTGRES_URI 格式无效") from exc
    base_driver = url.drivername.split("+", maxsplit=1)[0]
    if base_driver not in {"postgresql", "postgres"}:
        raise ValueError("POSTGRES_URI 必须使用 PostgreSQL 协议")
    return url


def sqlalchemy_postgres_uri(uri: str) -> str:
    """Return an async SQLAlchemy URL backed by psycopg 3."""

    return _postgres_url(uri).set(drivername="postgresql+psycopg").render_as_string(
        hide_password=False
    )


def psycopg_postgres_uri(uri: str) -> str:
    """Return a plain libpq-compatible URL for psycopg and its pool."""

    return _postgres_url(uri).set(drivername="postgresql").render_as_string(
        hide_password=False
    )


def session_factory() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _session_factory is not None
    return _session_factory


async def ensure_schema() -> None:
    """Create tables and the shared default user once per process."""

    global _initialized
    if _initialized:
        return
    async with _init_lock:
        if _initialized:
            return
        engine = get_engine()
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with session_factory()() as session:
            default_user = await session.get(User, DEFAULT_USER_ID)
            if default_user is None:
                session.add(
                    User(
                        id=DEFAULT_USER_ID,
                        username=DEFAULT_USERNAME,
                        username_normalized=DEFAULT_USERNAME,
                        password_hash=None,
                        is_system=True,
                    )
                )
                await session.commit()
        _initialized = True


async def close_database() -> None:
    """Dispose the process-scoped PostgreSQL application engine."""

    global _engine, _session_factory, _initialized
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
    _initialized = False


def _record(user: User) -> UserRecord:
    return UserRecord(
        id=user.id,
        username=user.username,
        is_system=user.is_system,
        password_hash=user.password_hash,
    )


def _email_delivery_record(delivery: EmailDelivery) -> EmailDeliveryRecord:
    return EmailDeliveryRecord(
        idempotency_key=delivery.idempotency_key,
        thread_id=delivery.thread_id,
        user_id=delivery.user_id,
        recipient=delivery.recipient,
        subject=delivery.subject,
        pdf_filename=delivery.pdf_filename,
        zip_filename=delivery.zip_filename,
        message_id=delivery.message_id,
        status=delivery.status,
        error_summary=delivery.error_summary,
    )


async def create_user(username: str, password_hash: str) -> UserRecord:
    await ensure_schema()
    user = User(
        id=str(uuid4()),
        username=username,
        username_normalized=username.lower(),
        password_hash=password_hash,
        is_system=False,
    )
    async with session_factory()() as session:
        session.add(user)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise UsernameExistsError("用户名已存在") from exc
    return _record(user)


async def get_user_by_username(username: str) -> UserRecord | None:
    await ensure_schema()
    async with session_factory()() as session:
        result = await session.execute(
            select(User).where(User.username_normalized == username.lower())
        )
        user = result.scalar_one_or_none()
    return _record(user) if user is not None else None


async def get_user_by_id(user_id: str) -> UserRecord | None:
    """Return one user without exposing its stored password hash."""

    await ensure_schema()
    async with session_factory()() as session:
        user = await session.get(User, str(user_id))
    if user is None:
        return None
    record = _record(user)
    return UserRecord(
        id=record.id,
        username=record.username,
        is_system=record.is_system,
    )


async def list_user_thread_ids(user_id: str) -> list[str]:
    """List every Agent Protocol thread claimed by one authenticated user."""

    await ensure_schema()
    async with session_factory()() as session:
        result = await session.execute(
            select(AgentThread.thread_id)
            .where(AgentThread.user_id == str(user_id))
            .order_by(AgentThread.created_at, AgentThread.thread_id)
        )
        return [str(thread_id) for thread_id in result.scalars().all()]


async def delete_user(user_id: str) -> bool:
    """Delete one non-system account after its external resources are purged.

    The evaluator deletes LangGraph checkpoints, sandboxes, and MongoDB state
    before calling this function. Explicit dependent-row deletes keep SQLite
    unit tests representative even when foreign-key cascades are disabled.
    """

    await ensure_schema()
    normalized = str(user_id)
    async with session_factory()() as session:
        user = await session.get(User, normalized)
        if user is None:
            return False
        if user.is_system or user.id == DEFAULT_USER_ID:
            raise ValueError("不能删除系统账户")
        await session.execute(delete(AuthSession).where(AuthSession.user_id == normalized))
        await session.execute(delete(EmailDelivery).where(EmailDelivery.user_id == normalized))
        await session.execute(delete(AgentThread).where(AgentThread.user_id == normalized))
        await session.execute(delete(User).where(User.id == normalized))
        await session.commit()
    return True


async def create_login_session(user_id: str) -> str:
    """Return the raw bearer token; only its digest is persisted."""

    await ensure_schema()
    token = secrets.token_urlsafe(48)
    expires_at = _utcnow() + timedelta(days=get_settings().auth_session_days)
    async with session_factory()() as session:
        session.add(
            AuthSession(
                id=str(uuid4()),
                user_id=user_id,
                token_hash=_token_digest(token),
                expires_at=expires_at,
            )
        )
        await session.commit()
    return token


async def resolve_login_session(token: str) -> UserRecord | None:
    await ensure_schema()
    async with session_factory()() as session:
        result = await session.execute(
            select(User)
            .join(AuthSession, AuthSession.user_id == User.id)
            .where(
                AuthSession.token_hash == _token_digest(token),
                AuthSession.revoked_at.is_(None),
                AuthSession.expires_at > _utcnow(),
            )
        )
        user = result.scalar_one_or_none()
    return _record(user) if user is not None else None


async def revoke_login_session(token: str) -> bool:
    await ensure_schema()
    async with session_factory()() as session:
        result = await session.execute(
            select(AuthSession).where(AuthSession.token_hash == _token_digest(token))
        )
        auth_session = result.scalar_one_or_none()
        if auth_session is None or auth_session.revoked_at is not None:
            return False
        auth_session.revoked_at = _utcnow()
        await session.commit()
    return True


async def consume_rate_limit(
    scope: str,
    raw_key: str,
    *,
    limit: int,
    window_seconds: int,
    now: datetime | None = None,
) -> RateLimitDecision:
    """Atomically consume one allowance from a PostgreSQL fixed window."""

    if not scope or not raw_key:
        raise ValueError("限流作用域和键不能为空")
    if limit < 1 or window_seconds < 1:
        raise ValueError("限流次数和窗口必须为正数")

    await ensure_schema()
    current = now or _utcnow()
    if current.tzinfo is not None:
        current = current.astimezone(UTC).replace(tzinfo=None)
    window_start, window_end = _rate_limit_window(current, window_seconds)
    key_hash = _rate_limit_key_hash(scope, raw_key)
    values = {
        "scope": scope,
        "key_hash": key_hash,
        "window_start": window_start,
        "count": 1,
        "expires_at": window_end,
    }

    async with session_factory()() as session:
        # PostgreSQL is authoritative in production; SQLite keeps unit tests
        # representative without adding a second external service dependency.
        dialect = session.get_bind().dialect.name
        if dialect == "postgresql":
            insert_statement = postgresql_insert(RateLimitBucket).values(**values)
        elif dialect == "sqlite":
            insert_statement = sqlite_insert(RateLimitBucket).values(**values)
        else:
            raise RuntimeError(f"不支持的限流数据库方言：{dialect}")

        await session.execute(
            delete(RateLimitBucket).where(RateLimitBucket.expires_at <= current)
        )
        statement = insert_statement.on_conflict_do_update(
            index_elements=[
                RateLimitBucket.scope,
                RateLimitBucket.key_hash,
                RateLimitBucket.window_start,
            ],
            set_={
                "count": RateLimitBucket.count + 1,
                "expires_at": window_end,
            },
        ).returning(RateLimitBucket.count)
        result = await session.execute(statement)
        count = int(result.scalar_one())
        await session.commit()

    retry_after = max(1, math.ceil((window_end - current).total_seconds()))
    return RateLimitDecision(
        allowed=count <= limit,
        count=count,
        limit=limit,
        retry_after_seconds=retry_after,
    )


async def clear_rate_limit(scope: str, raw_key: str) -> None:
    """Clear all active windows for one hashed key after successful login."""

    await ensure_schema()
    key_hash = _rate_limit_key_hash(scope, raw_key)
    async with session_factory()() as session:
        await session.execute(
            delete(RateLimitBucket).where(
                RateLimitBucket.scope == scope,
                RateLimitBucket.key_hash == key_hash,
            )
        )
        await session.commit()


async def claim_thread(thread_id: str, user_id: str) -> None:
    """Atomically bind a newly observed Agent Protocol thread to one user."""

    await ensure_schema()
    thread_id = str(thread_id)
    async with session_factory()() as session:
        existing = await session.get(AgentThread, thread_id)
        if existing is not None:
            if existing.user_id != user_id:
                raise ThreadOwnershipError("该会话不属于当前用户")
            return
        session.add(AgentThread(thread_id=thread_id, user_id=user_id))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            existing = await session.get(AgentThread, thread_id)
            if existing is None or existing.user_id != user_id:
                raise ThreadOwnershipError("该会话不属于当前用户") from None


async def get_thread_owner(thread_id: str) -> str | None:
    await ensure_schema()
    async with session_factory()() as session:
        thread = await session.get(AgentThread, str(thread_id))
        return thread.user_id if thread is not None else None


async def delete_thread_claim(thread_id: str, user_id: str) -> None:
    """Remove the ownership row after the Agent Server deletes its checkpoint."""

    await ensure_schema()
    async with session_factory()() as session:
        await session.execute(
            delete(AgentThread).where(
                AgentThread.thread_id == str(thread_id),
                AgentThread.user_id == user_id,
            )
        )
        await session.commit()


async def begin_email_delivery(
    *,
    idempotency_key: str,
    thread_id: str,
    user_id: str,
    recipient: str,
    subject: str,
    pdf_filename: str,
    zip_filename: str,
    message_id: str,
) -> tuple[EmailDeliveryRecord, bool]:
    """Create a sending record or return the existing replay-safe record."""

    await ensure_schema()
    async with session_factory()() as session:
        existing = await session.get(EmailDelivery, idempotency_key)
        if existing is not None:
            return _email_delivery_record(existing), False

        delivery = EmailDelivery(
            idempotency_key=idempotency_key,
            thread_id=thread_id,
            user_id=user_id,
            recipient=recipient,
            subject=subject,
            pdf_filename=pdf_filename,
            zip_filename=zip_filename,
            message_id=message_id,
            status="sending",
        )
        session.add(delivery)
        try:
            await session.commit()
        except IntegrityError:
            # A concurrent resume of the same tool call may win the unique insert.
            await session.rollback()
            existing = await session.get(EmailDelivery, idempotency_key)
            if existing is None:
                raise
            return _email_delivery_record(existing), False
        return _email_delivery_record(delivery), True


async def finish_email_delivery(
    idempotency_key: str,
    *,
    status: str,
    error_summary: str | None = None,
) -> EmailDeliveryRecord:
    """Persist one terminal delivery state without allowing arbitrary statuses."""

    if status not in {"sent", "failed", "uncertain"}:
        raise ValueError("邮件投递状态无效")
    await ensure_schema()
    async with session_factory()() as session:
        delivery = await session.get(EmailDelivery, idempotency_key)
        if delivery is None:
            raise RuntimeError("邮件投递记录不存在")
        delivery.status = status
        delivery.error_summary = error_summary[:255] if error_summary else None
        delivery.updated_at = _utcnow()
        await session.commit()
        return _email_delivery_record(delivery)
