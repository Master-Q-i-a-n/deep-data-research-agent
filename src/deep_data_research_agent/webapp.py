"""Custom FastAPI routes mounted by the LangGraph Agent Server."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import FastAPI, Header, HTTPException, Request
from langgraph_sdk import get_client
from pydantic import BaseModel, Field

from deep_data_research_agent import database
from deep_data_research_agent.auth import bearer_token
from deep_data_research_agent.memory import start_memory_worker

_USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,31}$")
_PASSWORD_HASHER = PasswordHasher()
_TERMINAL_TASK_STATUSES = frozenset(
    {"cancelled", "success", "error", "timeout", "interrupted"}
)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class AsyncTaskStatusRequest(BaseModel):
    """Identify the owning Supervisor thread; task IDs come from its state."""

    thread_id: str = Field(min_length=1, max_length=64)


def _user_payload(user: database.UserRecord) -> dict[str, object]:
    return {
        "id": user.id,
        "username": user.username,
        "is_default": user.is_system,
    }


async def _issue_token(user: database.UserRecord) -> dict[str, object]:
    return {
        "token": await database.create_login_session(user.id),
        "user": _user_payload(user),
    }


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    await database.ensure_schema()
    memory_worker = await start_memory_worker()
    # Loopback Agent Protocol calls query checkpoints and child runs without
    # creating a Supervisor model run.
    agent_client = get_client(url=None, api_key=None)
    _app.state.agent_client = agent_client
    try:
        yield
    finally:
        await agent_client.aclose()
        if memory_worker is not None:
            await memory_worker.stop()
        await database.close_database()


app = FastAPI(title="深研账户 API", lifespan=lifespan)


@app.post("/auth/register", status_code=201)
async def register(payload: RegisterRequest) -> dict[str, object]:
    username = payload.username.strip()
    if not _USERNAME_PATTERN.fullmatch(username):
        raise HTTPException(
            status_code=422,
            detail="用户名须为 3–32 位字母、数字、下划线或连字符",
        )
    if payload.password != payload.confirm_password:
        raise HTTPException(status_code=422, detail="两次输入的密码不一致")

    password_hash = await asyncio.to_thread(_PASSWORD_HASHER.hash, payload.password)
    try:
        user = await database.create_user(username, password_hash)
    except database.UsernameExistsError as exc:
        raise HTTPException(status_code=409, detail="用户名已存在") from exc
    return await _issue_token(user)


@app.post("/auth/login")
async def login(payload: LoginRequest) -> dict[str, object]:
    user = await database.get_user_by_username(payload.username.strip())
    if user is None or user.is_system or not user.password_hash:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    try:
        await asyncio.to_thread(
            _PASSWORD_HASHER.verify,
            user.password_hash,
            payload.password,
        )
    except (VerificationError, InvalidHashError) as exc:
        raise HTTPException(status_code=401, detail="用户名或密码错误") from exc
    return await _issue_token(user)


@app.post("/auth/logout")
async def logout(authorization: str | None = Header(default=None)) -> dict[str, str]:
    token = bearer_token(authorization)
    if token is not None:
        await database.revoke_login_session(token)
    return {"status": "logged_out"}


@app.get("/auth/me")
async def current_user(
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    token = bearer_token(authorization)
    if token is None:
        return {
            "user": {
                "id": database.DEFAULT_USER_ID,
                "username": "默认账户",
                "is_default": True,
            }
        }
    user = await database.resolve_login_session(token)
    if user is None:
        raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
    return {"user": _user_payload(user)}


@app.post("/async-tasks/status")
async def async_task_status(
    payload: AsyncTaskStatusRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict[str, object]:
    """Return live child-run statuses without invoking the Supervisor model."""

    token = bearer_token(authorization)
    if token is None:
        user_id = database.DEFAULT_USER_ID
    else:
        user = await database.resolve_login_session(token)
        if user is None:
            raise HTTPException(status_code=401, detail="登录已失效，请重新登录")
        user_id = user.id

    owner = await database.get_thread_owner(payload.thread_id)
    if owner != user_id:
        # Do not reveal whether a thread owned by another user exists.
        raise HTTPException(status_code=404, detail="会话不存在")

    client = request.app.state.agent_client
    try:
        parent_thread = await client.threads.get(thread_id=payload.thread_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail="暂时无法读取后台任务") from exc

    values = parent_thread.get("values") or {}
    tracked = values.get("async_tasks") if isinstance(values, dict) else None
    if not isinstance(tracked, dict):
        return {"tasks": []}

    async def inspect_task(task_id: str, raw_task: object) -> dict[str, object]:
        if not isinstance(raw_task, dict):
            return {"task_id": task_id, "status": "error", "poll_error": "任务记录格式无效"}

        task = dict(raw_task)
        task["task_id"] = str(task.get("task_id") or task_id)
        cached_status = str(task.get("status") or "running")
        if cached_status in _TERMINAL_TASK_STATUSES:
            return task

        child_thread_id = str(task.get("thread_id") or task["task_id"])
        run_id = str(task.get("run_id") or "")
        if not run_id:
            task["poll_error"] = "任务缺少 run_id"
            return task
        try:
            run = await client.runs.get(thread_id=child_thread_id, run_id=run_id)
            task["status"] = str(run.get("status") or cached_status)
        except Exception:  # noqa: BLE001 - keep the last known state on polling errors.
            task["poll_error"] = "暂时无法获取实时状态"
        return task

    tasks = await asyncio.gather(
        *(inspect_task(str(task_id), raw_task) for task_id, raw_task in tracked.items())
    )
    return {"tasks": tasks}
