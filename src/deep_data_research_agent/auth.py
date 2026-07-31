"""LangGraph authentication and per-user Agent Protocol authorization."""

from __future__ import annotations

from typing import Any

from langgraph_sdk import Auth
from starlette.requests import Request

from deep_data_research_agent import database

auth = Auth()


def bearer_token(authorization: str | None) -> str | None:
    """Parse an optional Bearer header and reject malformed credentials."""

    value = (authorization or "").strip()
    if not value:
        return None
    scheme, separator, token = value.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token.strip():
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="登录凭据格式无效",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token.strip()


async def authenticated_user(authorization: str | None) -> database.UserRecord:
    """Resolve a required login token for custom HTTP routes."""

    token = bearer_token(authorization)
    if token is None:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="请先登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = await database.resolve_login_session(token)
    if user is None:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="登录已失效，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


@auth.authenticate
async def authenticate_request(request: Request) -> Auth.types.MinimalUserDict:
    """Authenticate a request or attach the explicitly shared default account."""

    token = bearer_token(request.headers.get("authorization"))
    if token is None:
        await database.ensure_schema()
        return {
            "identity": database.DEFAULT_USER_ID,
            "display_name": "默认账户",
            "is_authenticated": False,
            "permissions": ["anonymous"],
        }

    user = await database.resolve_login_session(token)
    if user is None:
        raise Auth.exceptions.HTTPException(
            status_code=401,
            detail="登录已失效，请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return {
        "identity": user.id,
        "display_name": user.username,
        "is_authenticated": True,
        "permissions": ["authenticated"],
    }


def _owner_filter(ctx: Auth.types.AuthContext) -> dict[str, str]:
    return {"owner": ctx.user.identity}


@auth.on
async def deny_unhandled(ctx: Auth.types.AuthContext, value: Any) -> bool:
    """Deny resources not explicitly required by this application."""

    del ctx, value
    return False


@auth.on.threads.create
async def create_thread(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.create.value,
) -> None:
    metadata = value.setdefault("metadata", {})
    metadata["owner"] = ctx.user.identity
    thread_id = value.get("thread_id")
    if thread_id is not None:
        try:
            await database.claim_thread(str(thread_id), ctx.user.identity)
        except database.ThreadOwnershipError as exc:
            raise Auth.exceptions.HTTPException(status_code=404, detail="会话不存在") from exc


@auth.on.threads.create_run
async def create_run(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.create_run.value,
) -> dict[str, str]:
    metadata = value.setdefault("metadata", {})
    metadata["owner"] = ctx.user.identity
    thread_id = value.get("thread_id")
    if thread_id is not None:
        try:
            await database.claim_thread(str(thread_id), ctx.user.identity)
        except database.ThreadOwnershipError as exc:
            raise Auth.exceptions.HTTPException(status_code=404, detail="会话不存在") from exc
    return _owner_filter(ctx)


@auth.on.threads.read
async def read_thread(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.read.value,
) -> dict[str, str]:
    del value
    return _owner_filter(ctx)


@auth.on.threads.search
async def search_threads(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.search.value,
) -> dict[str, str]:
    del value
    return _owner_filter(ctx)


@auth.on.threads.update
async def update_thread(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.update.value,
) -> dict[str, str]:
    if "metadata" in value:
        value.setdefault("metadata", {})["owner"] = ctx.user.identity
    return _owner_filter(ctx)


@auth.on.threads.delete
async def delete_thread(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.threads.delete.value,
) -> dict[str, str]:
    del value
    return _owner_filter(ctx)


@auth.on.assistants.read
async def read_assistant(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.assistants.read.value,
) -> bool:
    """Assistants are application definitions and contain no user data."""

    del ctx, value
    return True


@auth.on.assistants.search
async def search_assistants(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.assistants.search.value,
) -> bool:
    del ctx, value
    return True


@auth.on.store
async def deny_public_store(
    ctx: Auth.types.AuthContext,
    value: Auth.types.on.store.value,
) -> bool:
    """Dynamic Skills are accessed inside graphs, never through public Store APIs."""

    del ctx, value
    return False
