"""Provision PostgreSQL roles and run deployment-owned schema migrations."""

from __future__ import annotations

import asyncio
import secrets
import sys
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from alembic.util.exc import CommandError
from dotenv import dotenv_values, set_key
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool, PoolTimeout
from sqlalchemy import String, create_engine, inspect
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from deep_data_research_agent.database import repository as database
from deep_data_research_agent.database.models import Base
from deep_data_research_agent.database.schema import ALEMBIC_BASELINE_REVISION

DATABASE_NAME = "deep_data_research_agent"
MIGRATOR_ROLE = "deep_data_research_agent_migrator"
APP_ROLE = "deep_data_research_agent_app"
ADMIN_ENV_PATH = Path(".env.postgres-admin")
APP_ENV_PATH = Path(".env")
PROJECT_ROOT = Path(__file__).resolve().parents[4]
MIGRATIONS_ROOT = Path(__file__).resolve().parents[2] / "database" / "migrations"


class SchemaCompatibilityError(RuntimeError):
    """Raised before ownership changes when a legacy schema is unsafe to stamp."""


def _password(values: dict[str, str | None], path: Path, name: str) -> str:
    configured = (values.get(name) or "").strip()
    if configured:
        return configured
    configured = secrets.token_urlsafe(32)
    # Generated secrets stay in the ignored administrator file and are stable
    # across idempotent setup reruns.
    set_key(str(path), name, configured, quote_mode="always")
    return configured


def _read_admin_settings(path: Path) -> tuple[str, str, str]:
    values = dict(dotenv_values(path))
    admin_uri = (values.get("POSTGRES_ADMIN_URI") or "").strip()
    if not admin_uri:
        raise RuntimeError(f"{path} 缺少 POSTGRES_ADMIN_URI")
    migrator_password = _password(values, path, "POSTGRES_MIGRATOR_PASSWORD")
    values = dict(dotenv_values(path))
    app_password = _password(values, path, "POSTGRES_APP_PASSWORD")
    return database.psycopg_postgres_uri(admin_uri), migrator_password, app_password


def _role_uri(admin_uri: str, role: str, password: str) -> str:
    url = make_url(admin_uri)
    return url.set(
        drivername="postgresql",
        username=role,
        password=password,
        database=DATABASE_NAME,
        query={key: value for key, value in url.query.items() if key != "options"},
    ).render_as_string(hide_password=False)


def _application_uri(admin_uri: str, password: str) -> str:
    return _role_uri(admin_uri, APP_ROLE, password)


def _migration_uri(admin_uri: str, password: str) -> str:
    return _role_uri(admin_uri, MIGRATOR_ROLE, password)


def _admin_database_uri(admin_uri: str) -> str:
    url = make_url(admin_uri)
    return url.set(
        drivername="postgresql",
        database=DATABASE_NAME,
        query={key: value for key, value in url.query.items() if key != "options"},
    ).render_as_string(hide_password=False)


def _create_or_update_role(connection: psycopg.Connection, role: str, password: str) -> None:
    existing = connection.execute(
        "SELECT 1 FROM pg_roles WHERE rolname = %s",
        (role,),
    ).fetchone()
    action = "ALTER ROLE" if existing else "CREATE ROLE"
    connection.execute(
        sql.SQL(
            f"{action} {{}} LOGIN PASSWORD {{}} NOSUPERUSER NOCREATEDB "
            "NOCREATEROLE NOREPLICATION"
        ).format(sql.Identifier(role), sql.Literal(password))
    )


def _create_roles_and_database(
    admin_uri: str,
    migrator_password: str,
    app_password: str,
) -> None:
    """Create identities and transfer the dedicated database to the migrator."""

    with psycopg.connect(admin_uri, autocommit=True, row_factory=dict_row) as connection:
        _create_or_update_role(connection, MIGRATOR_ROLE, migrator_password)
        _create_or_update_role(connection, APP_ROLE, app_password)
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
                    sql.Identifier(MIGRATOR_ROLE),
                )
            )
        elif existing["owner"] not in {APP_ROLE, MIGRATOR_ROLE}:
            raise RuntimeError(
                f"数据库 {DATABASE_NAME} 已存在，但所有者不是项目应用或迁移角色"
            )


def _transfer_database_ownership(admin_uri: str) -> None:
    """Transfer the dedicated database only after compatibility succeeds."""

    with psycopg.connect(admin_uri, autocommit=True) as connection:
        connection.execute(
            sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                sql.Identifier(DATABASE_NAME),
                sql.Identifier(MIGRATOR_ROLE),
            )
        )
    with psycopg.connect(
        _admin_database_uri(admin_uri), autocommit=True, row_factory=dict_row
    ) as connection:
        connection.execute(
            sql.SQL("REASSIGN OWNED BY {} TO {}").format(
                sql.Identifier(APP_ROLE), sql.Identifier(MIGRATOR_ROLE)
            )
        )
        connection.execute(
            sql.SQL("ALTER SCHEMA public OWNER TO {}").format(
                sql.Identifier(MIGRATOR_ROLE)
            )
        )


def _legacy_schema_issues(migration_uri: str) -> tuple[bool, list[str]]:
    """Return whether app tables exist and any unsafe baseline differences."""

    engine = create_engine(database.sqlalchemy_postgres_uri(migration_uri))
    try:
        inspector = inspect(engine)
        expected_tables = Base.metadata.tables
        existing_tables = set(inspector.get_table_names(schema="public"))
        present = bool(existing_tables.intersection(expected_tables))
        if not present:
            return False, []

        issues: list[str] = []
        for table_name, table in expected_tables.items():
            if table_name not in existing_tables:
                issues.append(f"缺少表 {table_name}")
                continue
            actual_columns = {
                column["name"]: column
                for column in inspector.get_columns(table_name, schema="public")
            }
            for expected in table.columns:
                actual = actual_columns.get(expected.name)
                if actual is None:
                    issues.append(f"{table_name} 缺少列 {expected.name}")
                    continue
                if bool(actual["nullable"]) != bool(expected.nullable):
                    issues.append(f"{table_name}.{expected.name} nullable 不兼容")
                if actual["type"]._type_affinity is not expected.type._type_affinity:
                    issues.append(f"{table_name}.{expected.name} 类型不兼容")
                if (
                    isinstance(expected.type, String)
                    and expected.type.length
                    and getattr(actual["type"], "length", None) != expected.type.length
                ):
                    issues.append(f"{table_name}.{expected.name} 长度不兼容")
            expected_pk = {column.name for column in table.primary_key.columns}
            actual_pk = set(
                inspector.get_pk_constraint(table_name, schema="public").get(
                    "constrained_columns"
                )
                or []
            )
            if expected_pk != actual_pk:
                issues.append(f"{table_name} 主键不兼容")
            actual_indexes = {
                (tuple(index.get("column_names") or []), bool(index.get("unique")))
                for index in inspector.get_indexes(table_name, schema="public")
            }
            for index in table.indexes:
                signature = (
                    tuple(column.name for column in index.columns),
                    bool(index.unique),
                )
                if signature not in actual_indexes:
                    issues.append(f"{table_name} 缺少索引 {index.name}")
            actual_foreign_keys = {
                (
                    tuple(item.get("constrained_columns") or []),
                    item.get("referred_table"),
                    tuple(item.get("referred_columns") or []),
                    str((item.get("options") or {}).get("ondelete", "")).upper(),
                )
                for item in inspector.get_foreign_keys(table_name, schema="public")
            }
            for constraint in table.foreign_key_constraints:
                signature = (
                    tuple(element.parent.name for element in constraint.elements),
                    next(iter(constraint.elements)).column.table.name,
                    tuple(element.column.name for element in constraint.elements),
                    str(constraint.ondelete or "").upper(),
                )
                if signature not in actual_foreign_keys:
                    issues.append(f"{table_name} 缺少外键 {signature[0]}")
        return True, issues
    finally:
        engine.dispose()


def _alembic_config(migration_uri: str) -> Config:
    config_path = PROJECT_ROOT / "alembic.ini"
    config = Config(str(config_path)) if config_path.is_file() else Config()
    config.set_main_option("script_location", str(MIGRATIONS_ROOT))
    config.set_main_option(
        "sqlalchemy.url",
        database.sqlalchemy_postgres_uri(migration_uri).replace("%", "%%"),
    )
    return config


def _migrate_application_schema(migration_uri: str) -> None:
    config = _alembic_config(migration_uri)
    engine = create_engine(database.sqlalchemy_postgres_uri(migration_uri))
    try:
        has_version = inspect(engine).has_table("alembic_version", schema="public")
    finally:
        engine.dispose()

    if not has_version:
        has_legacy_tables, issues = _legacy_schema_issues(migration_uri)
        if issues:
            raise SchemaCompatibilityError(
                "存量 PostgreSQL 结构无法安全接管：" + "；".join(issues)
            )
        if has_legacy_tables:
            # This is the only historical data repair formerly performed by
            # runtime schema setup. It remains deployment-owned and idempotent.
            engine = create_engine(database.sqlalchemy_postgres_uri(migration_uri))
            try:
                with engine.begin() as connection:
                    connection.exec_driver_sql(
                        """
                        UPDATE email_deliveries
                        SET status = 'uncertain',
                            error_summary = COALESCE(
                                error_summary, '升级前投递状态不确定'
                            ),
                            finished_at = COALESCE(finished_at, updated_at)
                        WHERE status = 'sending' AND pdf_path IS NULL
                        """
                    )
            finally:
                engine.dispose()
            command.stamp(config, ALEMBIC_BASELINE_REVISION)
    command.upgrade(config, "head")


async def _initialize_checkpoint_schema(migration_uri: str) -> None:
    pool = AsyncConnectionPool(
        conninfo=database.psycopg_postgres_uri(migration_uri),
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


def _grant_runtime_permissions(admin_uri: str) -> None:
    """Grant DML to the app role only after every deployment migration."""

    with psycopg.connect(_admin_database_uri(admin_uri), autocommit=True) as connection:
        statements = (
            sql.SQL("REVOKE CREATE ON SCHEMA public FROM PUBLIC"),
            sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(sql.Identifier(APP_ROLE)),
            sql.SQL("REVOKE CREATE, TEMP ON DATABASE {} FROM PUBLIC").format(sql.Identifier(DATABASE_NAME)),
            sql.SQL("REVOKE CREATE, TEMP ON DATABASE {} FROM {}").format(sql.Identifier(DATABASE_NAME), sql.Identifier(APP_ROLE)),
            sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {}").format(sql.Identifier(APP_ROLE)),
            sql.SQL("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {}").format(sql.Identifier(APP_ROLE)),
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(DATABASE_NAME), sql.Identifier(APP_ROLE)),
            sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(sql.Identifier(APP_ROLE)),
            sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {}").format(sql.Identifier(APP_ROLE)),
            sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {}").format(sql.Identifier(APP_ROLE)),
            sql.SQL("REVOKE INSERT, UPDATE, DELETE ON TABLE alembic_version FROM {}").format(sql.Identifier(APP_ROLE)),
            sql.SQL("REVOKE INSERT, UPDATE, DELETE ON TABLE checkpoint_migrations FROM {}").format(sql.Identifier(APP_ROLE)),
            sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {}").format(sql.Identifier(MIGRATOR_ROLE), sql.Identifier(APP_ROLE)),
            sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {}").format(sql.Identifier(MIGRATOR_ROLE), sql.Identifier(APP_ROLE)),
        )
        for statement in statements:
            connection.execute(statement)


def _write_application_settings(app_uri: str) -> None:
    if not APP_ENV_PATH.exists():
        APP_ENV_PATH.touch()
    set_key(str(APP_ENV_PATH), "POSTGRES_URI", app_uri, quote_mode="always")
    set_key(str(APP_ENV_PATH), "LANGGRAPH_STRICT_MSGPACK", "true", quote_mode="never")


def main() -> None:
    """Provision roles, migrate both schemas, and write runtime settings."""

    try:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        admin_uri, migrator_password, app_password = _read_admin_settings(ADMIN_ENV_PATH)
        _create_roles_and_database(admin_uri, migrator_password, app_password)
        admin_database_uri = _admin_database_uri(admin_uri)
        engine = create_engine(database.sqlalchemy_postgres_uri(admin_database_uri))
        try:
            has_version = inspect(engine).has_table("alembic_version", schema="public")
        finally:
            engine.dispose()
        if not has_version:
            _present, issues = _legacy_schema_issues(admin_database_uri)
            if issues:
                raise SchemaCompatibilityError(
                    "存量 PostgreSQL 结构无法安全接管：" + "；".join(issues)
                )
        _transfer_database_ownership(admin_uri)
        migration_uri = _migration_uri(admin_uri, migrator_password)
        _migrate_application_schema(migration_uri)
        asyncio.run(_initialize_checkpoint_schema(migration_uri))
        _grant_runtime_permissions(admin_uri)
        _write_application_settings(_application_uri(admin_uri, app_password))
    except SchemaCompatibilityError as exc:
        # Compatibility details contain only schema identifiers, not credentials.
        raise SystemExit(str(exc)) from None
    except (
        OSError,
        PoolTimeout,
        SQLAlchemyError,
        CommandError,
        psycopg.Error,
        RuntimeError,
        ValueError,
    ):
        # Never render connection URLs or generated passwords in terminal logs.
        raise SystemExit("PostgreSQL 初始化失败；请检查管理员配置和结构兼容性") from None
    print("PostgreSQL 迁移角色、运行角色、应用表和 checkpoint 表已初始化。")
    print("运行连接已写入本地 .env；迁移凭据只保留在管理员配置中。")


__all__ = ["main"]
