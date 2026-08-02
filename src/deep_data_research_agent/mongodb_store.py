"""LangGraph custom Store factory backed by MongoDB."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import cast

from langgraph.store.base import (
    BaseStore,
    GetOp,
    ListNamespacesOp,
    Op,
    PutOp,
    Result,
    SearchOp,
)
from langgraph.store.mongodb import MongoDBStore

from deep_data_research_agent.config import get_settings


class NamespaceRoutedStore(BaseStore):
    """Route preference namespaces to a dedicated Store collection."""

    def __init__(self, *, default: BaseStore, preferences: BaseStore) -> None:
        self._default = default
        self._preferences = preferences

    @staticmethod
    def _namespace(op: Op) -> tuple[str, ...] | None:
        if isinstance(op, (GetOp, PutOp)):
            return op.namespace
        if isinstance(op, SearchOp):
            return op.namespace_prefix
        return None

    @staticmethod
    def _is_preferences_namespace(namespace: tuple[str, ...]) -> bool:
        # Current namespaces are: (user_hash, "memories", "preferences").
        return len(namespace) >= 2 and namespace[1] == "memories"

    def _store_for(self, op: Op) -> BaseStore | None:
        namespace = self._namespace(op)
        if namespace is not None:
            return self._preferences if self._is_preferences_namespace(namespace) else self._default
        if isinstance(op, ListNamespacesOp) and op.match_conditions:
            prefixes = [
                tuple(condition.path)
                for condition in op.match_conditions
                if condition.match_type == "prefix"
            ]
            if prefixes and all(self._is_preferences_namespace(path) for path in prefixes):
                return self._preferences
            if prefixes and all(
                len(path) >= 2 and path[1] != "memories"
                for path in prefixes
            ):
                return self._default
        return None

    @staticmethod
    def _expanded_list_op(op: ListNamespacesOp) -> ListNamespacesOp:
        return op._replace(offset=0, limit=op.offset + op.limit)

    @staticmethod
    def _merge_namespace_results(
        op: ListNamespacesOp,
        first: Result,
        second: Result,
    ) -> Result:
        namespaces = sorted(set(cast("list[tuple[str, ...]]", first)) | set(cast("list[tuple[str, ...]]", second)))
        return namespaces[op.offset : op.offset + op.limit]

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        results: list[Result] = []
        for op in ops:
            store = self._store_for(op)
            if store is not None:
                results.append(store.batch([op])[0])
                continue
            if not isinstance(op, ListNamespacesOp):
                raise TypeError(f"不支持的 Store 操作：{type(op).__name__}")
            expanded = self._expanded_list_op(op)
            results.append(
                self._merge_namespace_results(
                    op,
                    self._default.batch([expanded])[0],
                    self._preferences.batch([expanded])[0],
                )
            )
        return results

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        async def run(op: Op) -> Result:
            store = self._store_for(op)
            if store is not None:
                return (await store.abatch([op]))[0]
            if not isinstance(op, ListNamespacesOp):
                raise TypeError(f"不支持的 Store 操作：{type(op).__name__}")
            expanded = self._expanded_list_op(op)
            first, second = await asyncio.gather(
                self._default.abatch([expanded]),
                self._preferences.abatch([expanded]),
            )
            return self._merge_namespace_results(op, first[0], second[0])

        return list(await asyncio.gather(*(run(op) for op in ops)))


def _migrate_preferences(
    skill_store: MongoDBStore,
    preferences_store: MongoDBStore,
) -> int:
    """Move legacy preference documents without overwriting newer target data."""

    migrated = 0
    legacy_documents = skill_store.collection.find(
        {"namespace.1": "memories", "namespace.2": "preferences"}
    )
    for document in legacy_documents:
        source_id = document.pop("_id")
        preferences_store.collection.update_one(
            {
                "namespace_str": document["namespace_str"],
                "key": document["key"],
            },
            {"$setOnInsert": document},
            upsert=True,
        )
        # Deleting only after the target write makes an interrupted migration resumable.
        skill_store.collection.delete_one({"_id": source_id})
        migrated += 1
    return migrated


@contextmanager
def create_mongodb_store() -> Iterator[NamespaceRoutedStore]:
    """Create one logical Store backed by separate Skill and preference collections."""

    settings = get_settings()
    if not settings.mongodb_uri.strip():
        raise RuntimeError("MONGODB_URI 未配置，无法初始化用户 Skill Store")

    if settings.mongodb_skill_collection == settings.mongodb_user_preferences_collection:
        raise RuntimeError("Skill 和用户偏好必须配置为不同的 MongoDB collection")

    with MongoDBStore.from_conn_string(
        conn_string=settings.mongodb_uri,
        db_name=settings.mongodb_database,
        collection_name=settings.mongodb_skill_collection,
    ) as skill_store, MongoDBStore.from_conn_string(
        conn_string=settings.mongodb_uri,
        db_name=settings.mongodb_database,
        collection_name=settings.mongodb_user_preferences_collection,
    ) as preferences_store:
        _migrate_preferences(skill_store, preferences_store)
        yield NamespaceRoutedStore(
            default=skill_store,
            preferences=preferences_store,
        )
