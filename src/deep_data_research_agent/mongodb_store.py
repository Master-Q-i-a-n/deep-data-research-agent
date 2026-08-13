"""LangGraph custom Store factory backed by MongoDB."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, cast

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
from deep_data_research_agent.skill_storage import (
    SKILL_AGENT_NAMES,
    SKILL_SEED_ROOT,
    file_store_value,
    public_skill_namespace,
)


class NamespaceRoutedStore(BaseStore):
    """Route all memory namespaces to the dedicated memory collection."""

    def __init__(self, *, default: BaseStore, memories: BaseStore) -> None:
        self._default = default
        self._memories = memories

    @staticmethod
    def _namespace(op: Op) -> tuple[str, ...] | None:
        if isinstance(op, (GetOp, PutOp)):
            return op.namespace
        if isinstance(op, SearchOp):
            return op.namespace_prefix
        return None

    @staticmethod
    def _is_memory_namespace(namespace: tuple[str, ...]) -> bool:
        # User and public Agent memories both use "memories" as component two.
        return len(namespace) >= 2 and namespace[1] == "memories"

    def _store_for(self, op: Op) -> BaseStore | None:
        namespace = self._namespace(op)
        if namespace is not None:
            return self._memories if self._is_memory_namespace(namespace) else self._default
        if isinstance(op, ListNamespacesOp) and op.match_conditions:
            prefixes = [
                tuple(condition.path)
                for condition in op.match_conditions
                if condition.match_type == "prefix"
            ]
            if prefixes and all(self._is_memory_namespace(path) for path in prefixes):
                return self._memories
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
                    self._memories.batch([expanded])[0],
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
                self._memories.abatch([expanded]),
            )
            return self._merge_namespace_results(op, first[0], second[0])

        return list(await asyncio.gather(*(run(op) for op in ops)))


def _search_all(store: BaseStore, namespace: tuple[str, ...]) -> list[Any]:
    """Read a complete namespace without relying on a Store-specific cursor."""

    items: list[Any] = []
    offset = 0
    while True:
        page = store.search(namespace, limit=100, offset=offset)
        if not page:
            break
        items.extend(page)
        if len(page) < 100:
            break
        offset += 100
    return items


def _list_all_namespaces(store: BaseStore) -> list[tuple[str, ...]]:
    """List every namespace in pages for stores with a default result limit."""

    namespaces: list[tuple[str, ...]] = []
    offset = 0
    while True:
        page = store.list_namespaces(limit=100, offset=offset)
        if not page:
            break
        namespaces.extend(page)
        if len(page) < 100:
            break
        offset += 100
    return namespaces


def _migrate_legacy_skill_namespaces(skill_store: BaseStore) -> int:
    """Move legacy assigned Skill files while preserving existing target values."""

    migrated = 0
    legacy_namespaces = [
        namespace
        for namespace in _list_all_namespaces(skill_store)
        if len(namespace) == 4
        and namespace[1:3] == ("skills", "assigned")
    ]
    for source in legacy_namespaces:
        target = (source[0], "skills", source[3])
        source_items = _search_all(skill_store, source)
        target_items = {
            item.key: item
            for item in _search_all(skill_store, target)
        }

        # Complete every target write before removing any source document. This
        # makes a failed or interrupted migration safe to retry on next startup.
        for item in source_items:
            target_item = target_items.get(item.key)
            if target_item is None or _item_modified_timestamp(item) > _item_modified_timestamp(target_item):
                skill_store.put(target, item.key, item.value)
                migrated += 1
        for item in source_items:
            skill_store.delete(source, item.key)
    return migrated


def _item_modified_timestamp(item: Any) -> float:
    """Return a comparable file timestamp, preferring StoreBackend metadata."""

    raw = item.value.get("modified_at")
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.timestamp()
        except ValueError:
            pass
    updated_at = getattr(item, "updated_at", None)
    if isinstance(updated_at, datetime):
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        return updated_at.timestamp()
    return float("-inf")


def _public_seed_values(agent_name: str) -> dict[str, dict[str, str]]:
    """Build the exact MongoDB file set for one Agent's versioned public seeds."""

    agent_root = SKILL_SEED_ROOT / agent_name
    if not agent_root.is_dir():
        raise RuntimeError(f"公共 Skill 种子目录不存在：{agent_root}")

    result: dict[str, dict[str, str]] = {}
    skill_files: dict[str, list[tuple[str, str]]] = {}
    for path in sorted(agent_root.rglob("*")):
        if (
            not path.is_file()
            or path.is_symlink()
            or "__pycache__" in path.parts
            or path.suffix in {".pyc", ".pyo"}
        ):
            continue
        relative = path.relative_to(agent_root).as_posix()
        content = path.read_bytes()
        result[f"/active/{relative}"] = file_store_value(content)
        skill_name = relative.split("/", maxsplit=1)[0]
        skill_files.setdefault(skill_name, []).append(
            (relative, result[f"/active/{relative}"]["sha256"])
        )

    if not skill_files:
        raise RuntimeError(f"公共 Skill 种子目录为空：{agent_root}")

    for skill_name, files in skill_files.items():
        if not any(path == f"{skill_name}/SKILL.md" for path, _sha256 in files):
            raise RuntimeError(
                f"公共 Skill 种子缺少根级 SKILL.md：{agent_root / skill_name}"
            )
        manifest = json.dumps(
            {
                "agent_name": agent_name,
                "skill_name": skill_name,
                "source": "public-seed",
                "file_count": len(files),
                "files": [
                    {"path": path, "sha256": sha256}
                    for path, sha256 in files
                ],
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        result[f"/manifests/{skill_name}.json"] = file_store_value(manifest)
    return result


def _sync_public_skills(skill_store: BaseStore) -> dict[str, int]:
    """Mirror repository public seeds into isolated MongoDB namespaces."""

    expected_by_agent = {
        agent_name: _public_seed_values(agent_name)
        for agent_name in sorted(SKILL_AGENT_NAMES)
    }
    existing_by_agent = {
        agent_name: {
            item.key: item
            for item in _search_all(
                skill_store,
                public_skill_namespace(agent_name),
            )
        }
        for agent_name in sorted(SKILL_AGENT_NAMES)
    }

    upserted = 0
    for agent_name, expected in expected_by_agent.items():
        namespace = public_skill_namespace(agent_name)
        existing = existing_by_agent[agent_name]
        for key, value in expected.items():
            current = existing.get(key)
            current_sha = current.value.get("sha256") if current else None
            if current_sha == value["sha256"]:
                continue
            created_at = current.value.get("created_at") if current else None
            if isinstance(created_at, str):
                value["created_at"] = created_at
            skill_store.put(namespace, key, value)
            upserted += 1

    # Deletions are deliberately delayed until every upsert for every Agent has
    # succeeded, so a partial seed update cannot first destroy valid old files.
    deleted = 0
    for agent_name, existing in existing_by_agent.items():
        namespace = public_skill_namespace(agent_name)
        stale_keys = set(existing) - set(expected_by_agent[agent_name])
        for key in sorted(stale_keys):
            skill_store.delete(namespace, key)
            deleted += 1
    return {"upserted": upserted, "deleted": deleted}


@contextmanager
def create_mongodb_store() -> Iterator[NamespaceRoutedStore]:
    """Create one logical Store backed by separate Skill and memory collections."""

    settings = get_settings()
    if not settings.mongodb_uri.strip():
        raise RuntimeError("MONGODB_URI 未配置，无法初始化用户 Skill Store")

    if settings.mongodb_skill_collection == settings.mongodb_memory_collection:
        raise RuntimeError("Skill 和长期记忆必须配置为不同的 MongoDB collection")

    with MongoDBStore.from_conn_string(
        conn_string=settings.mongodb_uri,
        db_name=settings.mongodb_database,
        collection_name=settings.mongodb_skill_collection,
    ) as skill_store, MongoDBStore.from_conn_string(
        conn_string=settings.mongodb_uri,
        db_name=settings.mongodb_database,
        collection_name=settings.mongodb_memory_collection,
    ) as memory_store:
        _migrate_legacy_skill_namespaces(skill_store)
        _sync_public_skills(skill_store)
        yield NamespaceRoutedStore(
            default=skill_store,
            memories=memory_store,
        )
