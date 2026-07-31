"""MySQL persistence for accounts, login sessions, and thread ownership."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, ForeignKey, String, select
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


def _utcnow() -> datetime:
    """Return naive UTC for predictable MySQL DATETIME comparisons."""

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


@dataclass(frozen=True, slots=True)
class UserRecord:
    """User information safe to return to authentication callers."""

    id: str
    username: str
    is_system: bool
    password_hash: str | None = None


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
        uri = get_settings().mysql_uri.strip()
        if not uri:
            raise RuntimeError("MYSQL_URI 未配置，无法使用账户与用户检查点")
        _engine = create_async_engine(uri, pool_pre_ping=True, pool_recycle=1800)
        _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


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
    """Dispose the process-scoped MySQL engine."""

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

