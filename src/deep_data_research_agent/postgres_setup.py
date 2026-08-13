"""Create the dedicated PostgreSQL role/database without exposing credentials."""

from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path

import psycopg
from dotenv import dotenv_values, set_key
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import create_async_engine

from deep_data_research_agent import database

DATABASE_NAME = "deep_data_research_agent"
APP_ROLE = "deep_data_research_agent_app"
ADMIN_ENV_PATH = Path(".env.postgres-admin")
APP_ENV_PATH = Path(".env")


def _read_admin_settings(path: Path) -> tuple[str, str]:
    values = dotenv_values(path)
    admin_uri = (values.get("POSTGRES_ADMIN_URI") or "").strip()
    if not admin_uri:
        raise RuntimeError(f"{path} 缺少 POSTGRES_ADMIN_URI")

    password = (values.get("POSTGRES_APP_PASSWORD") or "").strip()
    if not password:
        password = secrets.token_urlsafe(32)
        # Persist the generated secret only in the ignored admin file so reruns
        # remain idempotent and do not rotate the application password silently.
        set_key(str(path), "POSTGRES_APP_PASSWORD", password, quote_mode="always")
    return database.psycopg_postgres_uri(admin_uri), password


def _application_uri(admin_uri: str, password: str) -> str:
    url = make_url(admin_uri)
    return url.set(
        drivername="postgresql",
        username=APP_ROLE,
        password=password,
        database=DATABASE_NAME,
        # An admin connection may set a maintenance search_path. The application
        # database deliberately uses its default public schema.
        query={key: value for key, value in url.query.items() if key != "options"},
    ).render_as_string(hide_password=False)


def _create_role_and_database(admin_uri: str, password: str) -> None:
    with psycopg.connect(admin_uri, autocommit=True, row_factory=dict_row) as connection:
        role = connection.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = %s",
            (APP_ROLE,),
        ).fetchone()
        if role is None:
            connection.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(APP_ROLE),
                    sql.Literal(password),
                )
            )
        else:
            # The password comes from the ignored admin file, not command output.
            connection.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
                    sql.Identifier(APP_ROLE),
                    sql.Literal(password),
                )
            )

        existing = connection.execute(
            """
            SELECT d.datname, r.rolname AS owner
            FROM pg_database AS d
            JOIN pg_roles AS r ON r.oid = d.datdba
            WHERE d.datname = %s
            """,
            (DATABASE_NAME,),
        ).fetchone()
        if existing is None:
            connection.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(DATABASE_NAME),
                    sql.Identifier(APP_ROLE),
                )
            )
        elif existing["owner"] != APP_ROLE:
            raise RuntimeError(
                f"数据库 {DATABASE_NAME} 已存在，但不属于专用角色 {APP_ROLE}"
            )


async def _initialize_schema(app_uri: str) -> None:
    engine = create_async_engine(database.sqlalchemy_postgres_uri(app_uri))
    try:
        async with engine.begin() as connection:
            # This command initializes schema only; normal application startup
            # creates the shared default account through ensure_schema().
            await connection.run_sync(database.Base.metadata.create_all)
    finally:
        await engine.dispose()

    pool = AsyncConnectionPool(
        conninfo=database.psycopg_postgres_uri(app_uri),
        min_size=1,
        max_size=1,
        open=False,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
    )
    await pool.open(wait=True)
    try:
        await AsyncPostgresSaver(pool).setup()
    finally:
        await pool.close()


def _write_application_settings(app_uri: str) -> None:
    if not APP_ENV_PATH.exists():
        APP_ENV_PATH.touch()
    set_key(str(APP_ENV_PATH), "POSTGRES_URI", app_uri, quote_mode="always")
    set_key(
        str(APP_ENV_PATH),
        "LANGGRAPH_STRICT_MSGPACK",
        "true",
        quote_mode="never",
    )


def main() -> None:
    """Create the database, initialize schemas, and update the ignored .env."""

    try:
        if sys.platform == "win32":
            # psycopg async connections do not support Windows' Proactor loop.
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        admin_uri, password = _read_admin_settings(ADMIN_ENV_PATH)
        _create_role_and_database(admin_uri, password)
        app_uri = _application_uri(admin_uri, password)
        asyncio.run(_initialize_schema(app_uri))
        _write_application_settings(app_uri)
    except (OSError, PoolTimeout, SQLAlchemyError, psycopg.Error, RuntimeError, ValueError) as exc:
        # Avoid rendering connection URLs or generated passwords in terminal logs.
        raise SystemExit(f"PostgreSQL 初始化失败（{type(exc).__name__}）") from None
    print("PostgreSQL 角色、数据库、应用表和 checkpoint 表已初始化。")
    print("应用连接已写入本地 .env；终端未输出任何凭证。")
