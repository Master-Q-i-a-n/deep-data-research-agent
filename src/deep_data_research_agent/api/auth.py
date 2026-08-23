"""LangGraph authentication and per-user Agent Protocol authorization."""

from __future__ import annotations

import logging
from typing import Any

from langgraph_sdk import Auth
from starlette.requests import Request

from deep_data_research_agent.admissions import redis_limits
from deep_data_research_agent.core.config import get_settings
from deep_data_research_agent.database import repository as database

auth = Auth()
logger = logging.getLogger(__name__)


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
        # Auth-first mode still runs this hook for custom routes. Keep only the
        # credential entry points public; all other production routes fail shut.
        if request.url.path in {"/auth/register", "/auth/login", "/auth/logout"}:
            return {
                "identity": "public-auth",
                "display_name": "认证入口",
                "is_authenticated": False,
                "permissions": ["public-auth"],
            }
        if get_settings().app_env == "production":
            raise Auth.exceptions.HTTPException(
                status_code=401,
                detail="请先登录",
                headers={"WWW-Authenticate": "Bearer"},
            )
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
    if thread_id is None:
        raise Auth.exceptions.HTTPException(
            status_code=422,
            detail="持久化运行缺少 thread_id",
        )

    internal_marker = metadata.get("deep_data_internal")
    if isinstance(internal_marker, dict):
        graph_id = str(metadata.get("graph_id") or "")
        try:
            allowed_internal = await redis_limits.consume_internal_run_marker(
                internal_marker,
                user_id=ctx.user.identity,
                graph_id=graph_id,
            )
        except Exception as exc:
            logger.exception("内部子任务凭据存储不可用")
            raise Auth.exceptions.HTTPException(
                status_code=503,
                detail="请求保护服务暂不可用，请稍后重试",
                headers={"X-Error-Code": "RATE_LIMIT_SERVICE_UNAVAILABLE"},
            ) from exc
        if not allowed_internal:
            raise Auth.exceptions.HTTPException(
                status_code=403,
                detail="内部子任务凭据无效或已使用",
                headers={"X-Error-Code": "INVALID_INTERNAL_RUN_MARKER"},
            )
        metadata["token_budget_session_id"] = str(
            internal_marker.get("token_budget_session_id") or ""
        )
    else:
        ui_metadata = metadata.get("deep_data_ui")
        submission_id = (
            str(ui_metadata.get("submission_id") or "")
            if isinstance(ui_metadata, dict)
            else ""
        )
        if not submission_id:
            raise Auth.exceptions.HTTPException(
                status_code=403,
                detail="请先完成运行准入检查",
                headers={"X-Error-Code": "RUN_ADMISSION_REQUIRED"},
            )
        try:
            permit_result = await redis_limits.consume_run_permit(
                ctx.user.identity,
                submission_id,
                str(thread_id),
            )
        except Exception as exc:
            logger.exception("运行准入凭据存储不可用")
            raise Auth.exceptions.HTTPException(
                status_code=503,
                detail="请求保护服务暂不可用，请稍后重试",
                headers={"X-Error-Code": "RATE_LIMIT_SERVICE_UNAVAILABLE"},
            ) from exc
        if permit_result != "CONSUMED":
            status_code = 403 if permit_result == "THREAD_MISMATCH" else 409
            code = {
                "MISSING_OR_EXPIRED": "RUN_ADMISSION_EXPIRED",
                "ALREADY_USED": "RUN_ADMISSION_ALREADY_USED",
                "THREAD_MISMATCH": "RUN_ADMISSION_MISMATCH",
            }.get(permit_result, "RUN_ADMISSION_INVALID")
            raise Auth.exceptions.HTTPException(
                status_code=status_code,
                detail="运行准入凭据无效、已过期或已使用",
                headers={"X-Error-Code": code},
            )
        metadata["token_budget_session_id"] = submission_id

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
