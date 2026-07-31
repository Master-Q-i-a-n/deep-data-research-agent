"""LangGraph custom Store factory backed by MongoDB."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from langgraph.store.mongodb import MongoDBStore

from deep_data_research_agent.config import get_settings


@contextmanager
def create_mongodb_store() -> Iterator[MongoDBStore]:
    """Create the process-scoped MongoDB Store used by LangGraph Server."""

    settings = get_settings()
    if not settings.mongodb_uri.strip():
        raise RuntimeError("MONGODB_URI 未配置，无法初始化用户 Skill Store")

    with MongoDBStore.from_conn_string(
        conn_string=settings.mongodb_uri,
        db_name=settings.mongodb_database,
        collection_name=settings.mongodb_skill_collection,
    ) as store:
        yield store
