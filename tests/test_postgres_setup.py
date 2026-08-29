from __future__ import annotations

import inspect as python_inspect
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from dotenv import dotenv_values

from deep_data_research_agent.database import repository as database
from deep_data_research_agent.infrastructure.postgres import setup as postgres_setup
from deep_data_research_agent.infrastructure.postgres.checkpointer import (
    create_user_checkpointer,
)


def test_postgres_url_helpers_select_psycopg_driver() -> None:
    source = "postgresql://app:p%40ss@127.0.0.1:5432/agent"

    sqlalchemy_uri = database.sqlalchemy_postgres_uri(source)
    psycopg_uri = database.psycopg_postgres_uri(sqlalchemy_uri)

    assert sqlalchemy_uri.startswith("postgresql+psycopg://")
    assert psycopg_uri.startswith("postgresql://")
    assert "p%40ss" in psycopg_uri


def test_postgres_url_rejects_other_database_drivers() -> None:
    with pytest.raises(ValueError, match="PostgreSQL"):
        database.sqlalchemy_postgres_uri("sqlite+aiosqlite:///local.db")


def test_application_uri_keeps_host_and_uses_dedicated_identity() -> None:
    uri = postgres_setup._application_uri(
        "postgresql://postgres:admin@db.example:5544/postgres"
        "?sslmode=require&options=-csearch_path%3Dprivate",
        "generated-password",
    )

    assert uri.startswith("postgresql://deep_data_research_agent_app:")
    assert "@db.example:5544/deep_data_research_agent" in uri
    assert "sslmode=require" in uri
    assert "options=" not in uri
    assert "admin" not in uri


def test_missing_role_passwords_are_generated_once(tmp_path: Path) -> None:
    path = tmp_path / ".env.postgres-admin"
    path.write_text(
        "POSTGRES_ADMIN_URI=postgresql://postgres:admin@127.0.0.1/postgres\n",
        encoding="utf-8",
    )

    _, first_migrator, first_app = postgres_setup._read_admin_settings(path)
    _, second_migrator, second_app = postgres_setup._read_admin_settings(path)

    assert first_migrator == second_migrator
    assert first_app == second_app
    assert first_migrator and first_app
    values = dotenv_values(path)
    assert values["POSTGRES_MIGRATOR_PASSWORD"] == first_migrator
    assert values["POSTGRES_APP_PASSWORD"] == first_app


def test_migration_uri_uses_separate_owner_identity() -> None:
    uri = postgres_setup._migration_uri(
        "postgresql://postgres:admin@db.example:5544/postgres",
        "migration-password",
    )

    assert uri.startswith("postgresql://deep_data_research_agent_migrator:")
    assert "deep_data_research_agent_app" not in uri


def test_application_env_contains_no_admin_connection(
    monkeypatch,
    tmp_path: Path,
) -> None:
    app_env = tmp_path / ".env"
    monkeypatch.setattr(postgres_setup, "APP_ENV_PATH", app_env)

    postgres_setup._write_application_settings(
        "postgresql://deep_data_research_agent_app:secret@127.0.0.1/"
        "deep_data_research_agent"
    )
    values = dotenv_values(app_env)

    assert values["POSTGRES_URI"].startswith(
        "postgresql://deep_data_research_agent_app:"
    )
    assert values["LANGGRAPH_STRICT_MSGPACK"] == "true"
    assert "POSTGRES_ADMIN_URI" not in values


def test_packaged_alembic_head_matches_runtime_revision() -> None:
    config = postgres_setup._alembic_config(
        "postgresql://migrator:dummy@127.0.0.1/deep_data_research_agent"
    )
    script = ScriptDirectory.from_config(config)

    assert script.get_current_head() == "0002_user_model_provider"


def test_runtime_initializers_contain_no_schema_ddl() -> None:
    repository_source = python_inspect.getsource(database.ensure_schema)
    checkpointer_source = python_inspect.getsource(create_user_checkpointer)

    assert "create_all" not in repository_source
    assert "upgrade_schema" not in repository_source
    assert ".setup(" not in checkpointer_source
