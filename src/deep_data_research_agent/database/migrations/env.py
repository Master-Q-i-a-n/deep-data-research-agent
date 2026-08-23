"""Alembic environment for application-owned PostgreSQL tables."""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from deep_data_research_agent.database.models import Base
from deep_data_research_agent.database.repository import sqlalchemy_postgres_uri

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Require an explicit migration credential instead of the runtime role."""

    configured = os.getenv("POSTGRES_MIGRATION_URI", "").strip()
    if not configured:
        configured = config.get_main_option("sqlalchemy.url").strip()
    if not configured:
        raise RuntimeError("POSTGRES_MIGRATION_URI 未配置，无法执行 Alembic")
    return sqlalchemy_postgres_uri(configured)


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section) or {}
    # ConfigParser treats percent signs as interpolation markers.
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
