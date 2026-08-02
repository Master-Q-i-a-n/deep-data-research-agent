from types import SimpleNamespace

import pytest
from langgraph.store.memory import InMemoryStore

from deep_data_research_agent.mongodb_store import (
    NamespaceRoutedStore,
    _migrate_preferences,
)


def test_namespace_router_separates_skills_and_preferences() -> None:
    skill_store = InMemoryStore()
    preferences_store = InMemoryStore()
    store = NamespaceRoutedStore(
        default=skill_store,
        preferences=preferences_store,
    )
    skill_namespace = ("user-a", "skills", "assigned", "supervisor")
    preference_namespace = ("user-a", "memories", "preferences")

    store.put(skill_namespace, "/active/demo/SKILL.md", {"content": "skill"})
    store.put(preference_namespace, "/preferences.md", {"content": "preference"})

    assert skill_store.get(skill_namespace, "/active/demo/SKILL.md") is not None
    assert skill_store.get(preference_namespace, "/preferences.md") is None
    assert preferences_store.get(preference_namespace, "/preferences.md") is not None
    assert preferences_store.get(skill_namespace, "/active/demo/SKILL.md") is None
    assert set(store.list_namespaces()) == {skill_namespace, preference_namespace}


@pytest.mark.asyncio
async def test_namespace_router_supports_async_store_backend_calls() -> None:
    skill_store = InMemoryStore()
    preferences_store = InMemoryStore()
    store = NamespaceRoutedStore(
        default=skill_store,
        preferences=preferences_store,
    )
    namespace = ("user-a", "memories", "preferences")

    await store.aput(namespace, "/preferences.md", {"content": "preference"})

    assert await preferences_store.aget(namespace, "/preferences.md") is not None
    assert await skill_store.aget(namespace, "/preferences.md") is None


def test_legacy_preferences_are_moved_after_target_upsert() -> None:
    document = {
        "_id": "legacy-id",
        "namespace": ["user-a", "memories", "preferences"],
        "namespace_str": "user-a/memories/preferences",
        "key": "/preferences.md",
        "value": {"content": "old"},
    }

    class SourceCollection:
        def __init__(self) -> None:
            self.documents = [document]

        def find(self, query):
            assert query == {"namespace.1": "memories", "namespace.2": "preferences"}
            return [dict(item) for item in self.documents]

        def delete_one(self, query):
            assert query == {"_id": "legacy-id"}
            self.documents.clear()

    class TargetCollection:
        def __init__(self) -> None:
            self.upserts: list[tuple[dict, dict, bool]] = []

        def update_one(self, query, update, *, upsert):
            self.upserts.append((query, update, upsert))

    source = SourceCollection()
    target = TargetCollection()

    count = _migrate_preferences(
        SimpleNamespace(collection=source),
        SimpleNamespace(collection=target),
    )

    assert count == 1
    assert source.documents == []
    assert target.upserts[0][0] == {
        "namespace_str": "user-a/memories/preferences",
        "key": "/preferences.md",
    }
    assert target.upserts[0][1]["$setOnInsert"]["value"] == {"content": "old"}
    assert target.upserts[0][2] is True
