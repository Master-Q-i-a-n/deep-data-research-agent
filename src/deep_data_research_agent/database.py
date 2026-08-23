"""PostgreSQL persistence for accounts, sessions, ownership, and deliveries."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, String, delete, select
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


class UserTokenBucket(Base):
    """Authoritative per-user token balance; Redis only caches this state."""

    __tablename__ = "user_token_buckets"

    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    balance_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_refill_hour: Mapped[int] = mapped_column(BigInteger, nullable=False)
    version: Mapped[int] = mapped_column(BigInteger, default=1, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


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
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


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
class TokenBucketRecord:
    user_id: str
    balance_tokens: int
    last_refill_hour: int
    version: int


@dataclass(frozen=True, slots=True)
class TokenUsageReservation:
    call_id: str
    bucket: TokenBucketRecord
    reserved_tokens: int
    created: bool


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
            user_ids = list((await session.execute(select(User.id))).scalars())
            existing_bucket_ids = set(
                (await session.execute(select(UserTokenBucket.user_id))).scalars()
            )
            settings = get_settings()
            current_hour = _current_refill_hour()
            for user_id in user_ids:
                if user_id not in existing_bucket_ids:
                    session.add(
                        UserTokenBucket(
                            user_id=user_id,
                            balance_tokens=settings.token_bucket_capacity,
                            last_refill_hour=current_hour,
                            version=1,
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


def _current_refill_hour(now: datetime | None = None) -> int:
    current = now or _utcnow()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return int(current.timestamp() // 3600)


def _refill_token_bucket(bucket: UserTokenBucket, current_hour: int) -> bool:
    """Apply whole-hour refills, including repayment of a negative balance."""

    elapsed_hours = max(0, current_hour - bucket.last_refill_hour)
    if elapsed_hours == 0:
        return False
    settings = get_settings()
    bucket.balance_tokens = min(
        settings.token_bucket_capacity,
        bucket.balance_tokens + elapsed_hours * settings.token_bucket_refill_per_hour,
    )
    bucket.last_refill_hour = current_hour
    bucket.version += 1
    bucket.updated_at = _utcnow()
    return True


def _token_bucket_record(bucket: UserTokenBucket) -> TokenBucketRecord:
    return TokenBucketRecord(
        user_id=bucket.user_id,
        balance_tokens=int(bucket.balance_tokens),
        last_refill_hour=int(bucket.last_refill_hour),
        version=int(bucket.version),
    )


async def _locked_token_bucket(session: AsyncSession, user_id: str) -> UserTokenBucket:
    result = await session.execute(
        select(UserTokenBucket)
        .where(UserTokenBucket.user_id == str(user_id))
        .with_for_update()
    )
    bucket = result.scalar_one_or_none()
    if bucket is None:
        # The foreign key also prevents creating buckets for fabricated identities.
        if await session.get(User, str(user_id)) is None:
            raise ValueError("Token 桶用户不存在")
        settings = get_settings()
        bucket = UserTokenBucket(
            user_id=str(user_id),
            balance_tokens=settings.token_bucket_capacity,
            last_refill_hour=_current_refill_hour(),
            version=1,
        )
        session.add(bucket)
        await session.flush()
    return bucket


async def get_token_bucket(user_id: str) -> TokenBucketRecord:
    """Return a refreshed authoritative snapshot for Redis admission checks."""

    await ensure_schema()
    async with session_factory()() as session:
        async with session.begin():
            bucket = await _locked_token_bucket(session, user_id)
            _refill_token_bucket(bucket, _current_refill_hour())
        return _token_bucket_record(bucket)


async def reserve_model_tokens(
    *,
    call_id: str,
    user_id: str,
    root_run_id: str | None,
    thread_id: str | None,
    agent_name: str,
    model_name: str,
    reserved_tokens: int,
) -> TokenUsageReservation:
    """Persist one reservation and debit its user in the same transaction."""

    if reserved_tokens < 1:
        raise ValueError("模型 Token 预占量必须为正数")
    await ensure_schema()
    async with session_factory()() as session:
        async with session.begin():
            existing = await session.get(ModelTokenUsage, call_id)
            bucket = await _locked_token_bucket(session, user_id)
            _refill_token_bucket(bucket, _current_refill_hour())
            if existing is not None:
                if existing.user_id != str(user_id):
                    raise ValueError("模型调用 ID 已属于其他用户")
                return TokenUsageReservation(
                    call_id=call_id,
                    bucket=_token_bucket_record(bucket),
                    reserved_tokens=int(existing.reserved_tokens),
                    created=False,
                )
            usage = ModelTokenUsage(
                call_id=call_id,
                user_id=str(user_id),
                root_run_id=root_run_id,
                thread_id=thread_id,
                agent_name=agent_name,
                model_name=model_name,
                reserved_tokens=reserved_tokens,
            )
            session.add(usage)
            bucket.balance_tokens -= reserved_tokens
            bucket.version += 1
            bucket.updated_at = _utcnow()
        return TokenUsageReservation(
            call_id=call_id,
            bucket=_token_bucket_record(bucket),
            reserved_tokens=reserved_tokens,
            created=True,
        )


async def settle_model_tokens(
    *,
    call_id: str,
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int,
    usage_source: str,
    status: str = "settled",
) -> TokenBucketRecord:
    """Finalize actual usage once and reconcile the earlier reservation."""

    if total_tokens < 0 or usage_source not in {"provider", "estimated", "reserved"}:
        raise ValueError("模型 Token 结算数据无效")
    if status not in {"settled", "failed"}:
        raise ValueError("模型 Token 结算状态无效")
    await ensure_schema()
    async with session_factory()() as session:
        async with session.begin():
            usage = await session.get(ModelTokenUsage, call_id, with_for_update=True)
            if usage is None:
                raise RuntimeError("模型 Token 预占记录不存在")
            bucket = await _locked_token_bucket(session, usage.user_id)
            _refill_token_bucket(bucket, _current_refill_hour())
            if usage.status != "pending":
                return _token_bucket_record(bucket)
            settings = get_settings()
            bucket.balance_tokens = min(
                settings.token_bucket_capacity,
                bucket.balance_tokens + int(usage.reserved_tokens) - total_tokens,
            )
            bucket.version += 1
            bucket.updated_at = _utcnow()
            usage.input_tokens = input_tokens
            usage.output_tokens = output_tokens
            usage.total_tokens = total_tokens
            usage.usage_source = usage_source
            usage.status = status
            usage.settled_at = _utcnow()
        return _token_bucket_record(bucket)


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
        session.add(
            UserTokenBucket(
                user_id=user.id,
                balance_tokens=get_settings().token_bucket_capacity,
                last_refill_hour=_current_refill_hour(),
                version=1,
            )
        )
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
        await session.execute(delete(ModelTokenUsage).where(ModelTokenUsage.user_id == normalized))
        await session.execute(delete(UserTokenBucket).where(UserTokenBucket.user_id == normalized))
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
