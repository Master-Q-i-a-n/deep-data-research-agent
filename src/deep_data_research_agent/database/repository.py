"""PostgreSQL persistence for accounts, sessions, ownership, and deliveries."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from deep_data_research_agent.core.config import get_settings
from deep_data_research_agent.database.models import (
    AgentThread,
    AuthSession,
    EmailDelivery,
    ModelTokenUsage,
    User,
    UserModelProvider,
    UserTokenBucket,
)
from deep_data_research_agent.database.models import (
    utcnow as _utcnow,
)
from deep_data_research_agent.database.schema import (
    ALEMBIC_HEAD_REVISION,
    CHECKPOINT_TABLES,
)

DEFAULT_USER_ID = "local-user"
DEFAULT_USERNAME = "default"
logger = logging.getLogger(__name__)


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
    pdf_path: str | None
    markdown_path: str | None
    message_id: str
    status: str
    attempts: int
    available_at: datetime
    lease_until: datetime | None
    finished_at: datetime | None
    error_summary: str | None
    created_at: datetime
    updated_at: datetime


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


@dataclass(frozen=True, slots=True)
class ModelProviderRecord:
    """Encrypted Provider row; decryption belongs to the Provider service."""

    user_id: str
    provider_type: str
    base_url: str
    model_name: str
    api_key_ciphertext: str
    api_key_hint: str
    version: int
    created_at: datetime
    updated_at: datetime


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


async def _validate_deployed_schema(*, include_checkpoints: bool = True) -> None:
    """Verify deployment-owned schemas without executing any DDL."""

    async with get_engine().connect() as connection:
        revision = (
            await connection.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()
        if revision != ALEMBIC_HEAD_REVISION:
            raise RuntimeError(
                "PostgreSQL 应用结构未迁移到当前版本；请先执行 setup-agent-postgres"
            )
        if include_checkpoints:
            for table_name in CHECKPOINT_TABLES:
                exists = (
                    await connection.execute(
                        text("SELECT to_regclass(:table_name)"),
                        {"table_name": f"public.{table_name}"},
                    )
                ).scalar_one()
                if exists is None:
                    raise RuntimeError(
                        "LangGraph checkpoint 结构未初始化；请先执行 setup-agent-postgres"
                    )
                # Names come from the packaged constant, never request input.
                await connection.execute(
                    text(f'SELECT 1 FROM "{table_name}" LIMIT 0')
                )


async def ensure_schema() -> None:
    """Validate deployed schemas and initialize shared rows once per process.

    The historical name remains temporarily stable for callers, but this
    function deliberately performs no DDL. Schema changes belong to Alembic.
    """

    global _initialized
    if _initialized:
        return
    async with _init_lock:
        if _initialized:
            return
        await _validate_deployed_schema()
        async with session_factory()() as session:
            # PostgreSQL upserts keep multi-instance startup race-free while
            # preserving this initialization as DML-only.
            now = _utcnow()
            await session.execute(
                text(
                    """
                    INSERT INTO users (
                        id, username, username_normalized, password_hash,
                        is_system, created_at
                    )
                    VALUES (
                        :id, :username, :username_normalized, NULL,
                        :is_system, :created_at
                    )
                    ON CONFLICT DO NOTHING
                    """
                ),
                {
                    "id": DEFAULT_USER_ID,
                    "username": DEFAULT_USERNAME,
                    "username_normalized": DEFAULT_USERNAME,
                    "is_system": True,
                    "created_at": now,
                },
            )
            settings = get_settings()
            await session.execute(
                text(
                    """
                    INSERT INTO user_token_buckets (
                        user_id, balance_tokens, last_refill_hour, version, updated_at
                    )
                    SELECT id, :capacity, :current_hour, 1, :updated_at
                    FROM users
                    WHERE 1 = 1
                    ON CONFLICT (user_id) DO NOTHING
                    """
                ),
                {
                    "capacity": settings.token_bucket_capacity,
                    "current_hour": _current_refill_hour(),
                    "updated_at": now,
                },
            )
            await session.commit()
        _initialized = True


async def check_database_ready() -> None:
    """Perform the uncached PostgreSQL readiness checks."""

    await _validate_deployed_schema()


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
        pdf_path=delivery.pdf_path,
        markdown_path=delivery.markdown_path,
        message_id=delivery.message_id,
        status=delivery.status,
        attempts=int(delivery.attempts),
        available_at=delivery.available_at,
        lease_until=delivery.lease_until,
        finished_at=delivery.finished_at,
        error_summary=delivery.error_summary,
        created_at=delivery.created_at,
        updated_at=delivery.updated_at,
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


def _model_provider_record(provider: UserModelProvider) -> ModelProviderRecord:
    return ModelProviderRecord(
        user_id=provider.user_id,
        provider_type=provider.provider_type,
        base_url=provider.base_url,
        model_name=provider.model_name,
        api_key_ciphertext=provider.api_key_ciphertext,
        api_key_hint=provider.api_key_hint,
        version=int(provider.version),
        created_at=provider.created_at,
        updated_at=provider.updated_at,
    )


async def get_model_provider(user_id: str) -> ModelProviderRecord | None:
    """Return one user's encrypted Provider row without decrypting its key."""

    await ensure_schema()
    async with session_factory()() as session:
        provider = await session.get(UserModelProvider, str(user_id))
    return _model_provider_record(provider) if provider is not None else None


async def upsert_model_provider(
    *,
    user_id: str,
    provider_type: str,
    base_url: str,
    model_name: str,
    api_key_ciphertext: str | None,
    api_key_hint: str | None,
) -> ModelProviderRecord:
    """Create or update the user's Provider while optionally retaining its key."""

    await ensure_schema()
    async with session_factory()() as session, session.begin():
        provider = await session.get(
            UserModelProvider,
            str(user_id),
            with_for_update=True,
        )
        now = _utcnow()
        if provider is None:
            if not api_key_ciphertext or api_key_hint is None:
                raise ValueError("首次配置模型 Provider 时必须填写 API Key")
            provider = UserModelProvider(
                user_id=str(user_id),
                provider_type=provider_type,
                base_url=base_url,
                model_name=model_name,
                api_key_ciphertext=api_key_ciphertext,
                api_key_hint=api_key_hint,
                version=1,
                created_at=now,
                updated_at=now,
            )
            session.add(provider)
        else:
            provider.provider_type = provider_type
            provider.base_url = base_url
            provider.model_name = model_name
            if api_key_ciphertext is not None:
                provider.api_key_ciphertext = api_key_ciphertext
                provider.api_key_hint = api_key_hint or ""
            provider.version += 1
            provider.updated_at = now
        await session.flush()
        record = _model_provider_record(provider)
    return record


async def delete_model_provider(user_id: str) -> bool:
    """Delete one user's Provider configuration."""

    await ensure_schema()
    async with session_factory()() as session:
        result = await session.execute(
            delete(UserModelProvider).where(UserModelProvider.user_id == str(user_id))
        )
        await session.commit()
    return bool(result.rowcount)


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
        await session.execute(
            delete(UserModelProvider).where(UserModelProvider.user_id == normalized)
        )
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
    pdf_path: str | None = None,
    markdown_path: str | None = None,
) -> tuple[EmailDeliveryRecord, bool]:
    """Create a queued outbox row or return the existing replay-safe record."""

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
            pdf_path=pdf_path,
            markdown_path=markdown_path,
            message_id=message_id,
            status="queued",
            attempts=0,
            available_at=_utcnow(),
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


async def get_email_delivery(
    idempotency_key: str,
    *,
    user_id: str | None = None,
) -> EmailDeliveryRecord | None:
    """Return one delivery, optionally enforcing its authenticated owner."""

    await ensure_schema()
    async with session_factory()() as session:
        delivery = await session.get(EmailDelivery, idempotency_key)
        if delivery is None or (user_id is not None and delivery.user_id != user_id):
            return None
        return _email_delivery_record(delivery)


async def claim_email_delivery(
    idempotency_key: str,
    *,
    lease_seconds: int = 300,
) -> EmailDeliveryRecord | None:
    """Claim one due delivery; duplicate Celery messages become harmless no-ops."""

    await ensure_schema()
    now = _utcnow()
    async with session_factory()() as session:
        async with session.begin():
            result = await session.execute(
                select(EmailDelivery)
                .where(EmailDelivery.idempotency_key == idempotency_key)
                .with_for_update()
            )
            delivery = result.scalar_one_or_none()
            if delivery is None:
                return None
            due = delivery.status in {"queued", "retry"} and delivery.available_at <= now
            stale = (
                delivery.status == "processing"
                and delivery.lease_until is not None
                and delivery.lease_until <= now
            )
            if not due and not stale:
                return None
            delivery.status = "processing"
            delivery.attempts += 1
            delivery.lease_until = now + timedelta(seconds=lease_seconds)
            delivery.updated_at = now
            delivery.error_summary = None
        return _email_delivery_record(delivery)


async def mark_email_submitting(
    idempotency_key: str,
    *,
    lease_seconds: int = 120,
) -> EmailDeliveryRecord:
    """Persist the no-blind-retry boundary immediately before SMTP submission."""

    await ensure_schema()
    now = _utcnow()
    async with session_factory()() as session:
        async with session.begin():
            delivery = await session.get(
                EmailDelivery,
                idempotency_key,
                with_for_update=True,
            )
            if delivery is None or delivery.status != "processing":
                raise RuntimeError("邮件投递未处于可提交状态")
            delivery.status = "submitting"
            delivery.lease_until = now + timedelta(seconds=lease_seconds)
            delivery.updated_at = now
        return _email_delivery_record(delivery)


async def schedule_email_retry(
    idempotency_key: str,
    *,
    delay_seconds: int,
    error_summary: str,
    max_attempts: int = 3,
) -> tuple[EmailDeliveryRecord, int | None]:
    """Schedule only an explicitly safe pre-submit retry."""

    await ensure_schema()
    now = _utcnow()
    async with session_factory()() as session:
        async with session.begin():
            delivery = await session.get(
                EmailDelivery,
                idempotency_key,
                with_for_update=True,
            )
            if delivery is None:
                raise RuntimeError("邮件投递记录不存在")
            if delivery.status in {"sent", "failed", "uncertain"}:
                return _email_delivery_record(delivery), None
            delivery.error_summary = error_summary[:255]
            delivery.lease_until = None
            delivery.updated_at = now
            if delivery.attempts >= max_attempts:
                delivery.status = "failed"
                delivery.finished_at = now
                retry_after = None
            else:
                delivery.status = "retry"
                delivery.available_at = now + timedelta(seconds=delay_seconds)
                retry_after = delay_seconds
        return _email_delivery_record(delivery), retry_after


async def recover_email_deliveries(*, limit: int = 100) -> list[str]:
    """Recover lost broker messages and classify stale SMTP submissions."""

    await ensure_schema()
    now = _utcnow()
    publish_ids: list[str] = []
    async with session_factory()() as session, session.begin():
        result = await session.execute(
                select(EmailDelivery)
                .where(
                    (
                        EmailDelivery.status.in_(["queued", "retry"])
                        & (EmailDelivery.available_at <= now)
                    )
                    | (
                    EmailDelivery.status.in_(["processing", "submitting"])
                    & (EmailDelivery.lease_until <= now)
                )
            )
            .order_by(EmailDelivery.created_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        for delivery in result.scalars():
            if delivery.status == "submitting":
                # A crashed worker may have handed the message to SMTP.
                delivery.status = "uncertain"
                delivery.error_summary = "SMTP 提交阶段中断，未自动重发"
                delivery.finished_at = now
                delivery.lease_until = None
                delivery.updated_at = now
                continue
            if delivery.status == "processing":
                delivery.status = "retry"
                delivery.available_at = now
                delivery.lease_until = None
                delivery.updated_at = now
            if delivery.available_at <= now:
                publish_ids.append(delivery.idempotency_key)
    return publish_ids


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
        async with session.begin():
            delivery = await session.get(
                EmailDelivery,
                idempotency_key,
                with_for_update=True,
            )
            if delivery is None:
                raise RuntimeError("邮件投递记录不存在")
            # Never downgrade a known terminal outcome on duplicate delivery.
            if delivery.status in {"sent", "failed", "uncertain"}:
                return _email_delivery_record(delivery)
            now = _utcnow()
            delivery.status = status
            delivery.error_summary = error_summary[:255] if error_summary else None
            delivery.lease_until = None
            delivery.finished_at = now
            delivery.updated_at = now
        return _email_delivery_record(delivery)
