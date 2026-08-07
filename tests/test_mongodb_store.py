from types import SimpleNamespace

import pytest
from langgraph.store.memory import InMemoryStore

from deep_data_research_agent import mongodb_store
from deep_data_research_agent.mongodb_store import (
    NamespaceRoutedStore,
    _migrate_legacy_skill_namespaces,
    _migrate_preferences,
    _sync_public_skills,
)


def test_namespace_router_separates_skills_and_preferences() -> None:
    skill_store = InMemoryStore()
    preferences_store = InMemoryStore()
    store = NamespaceRoutedStore(
        default=skill_store,
        preferences=preferences_store,
    )
    skill_namespace = ("user-a", "skills", "supervisor")
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


def test_legacy_skill_namespace_migration_preserves_target() -> None:
    store = InMemoryStore()
    source = ("user-a", "skills", "assigned", "data-analyst")
    target = ("user-a", "skills", "data-analyst")
    store.put(source, "/active/demo/SKILL.md", {"content": "old"})
    store.put(source, "/active/demo/run.py", {"content": "script"})
    store.put(target, "/active/demo/SKILL.md", {"content": "new"})

    assert _migrate_legacy_skill_namespaces(store) == 1
    assert store.get(target, "/active/demo/SKILL.md").value == {"content": "new"}
    assert store.get(target, "/active/demo/run.py").value == {"content": "script"}
    assert store.search(source) == []
    assert _migrate_legacy_skill_namespaces(store) == 0


def test_legacy_skill_namespace_migrates_newer_source_value() -> None:
    store = InMemoryStore()
    source = ("user-a", "skills", "assigned", "supervisor")
    target = ("user-a", "skills", "supervisor")
    store.put(
        target,
        "/active/demo/SKILL.md",
        {"content": "target-old", "modified_at": "2026-01-01T00:00:00+00:00"},
    )
    store.put(
        source,
        "/active/demo/SKILL.md",
        {"content": "source-new", "modified_at": "2026-02-01T00:00:00+00:00"},
    )

    assert _migrate_legacy_skill_namespaces(store) == 1
    assert store.get(target, "/active/demo/SKILL.md").value["content"] == "source-new"


def _write_seed(root, agent_name: str, skill_name: str, content: str) -> None:
    path = root / agent_name / skill_name / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_public_seed_sync_is_idempotent_updates_and_deletes(
    tmp_path,
    monkeypatch,
) -> None:
    for agent_name in ("supervisor", "data-analyst", "crawl-worker"):
        _write_seed(tmp_path, agent_name, "demo", f"{agent_name}-v1")
    monkeypatch.setattr(mongodb_store, "SKILL_SEED_ROOT", tmp_path)
    store = InMemoryStore()

    first = _sync_public_skills(store)
    second = _sync_public_skills(store)
    assert first == {"upserted": 6, "deleted": 0}
    assert second == {"upserted": 0, "deleted": 0}

    supervisor_file = tmp_path / "supervisor" / "demo" / "SKILL.md"
    supervisor_file.write_text("supervisor-v2", encoding="utf-8")
    stale_namespace = ("public", "skills", "data-analyst")
    store.put(stale_namespace, "/active/stale/SKILL.md", {"content": "stale"})

    changed = _sync_public_skills(store)
    # The content file and its hash manifest are updated together.
    assert changed == {"upserted": 2, "deleted": 1}
    assert store.get(stale_namespace, "/active/stale/SKILL.md") is None
    assert store.get(
        ("public", "skills", "supervisor"),
        "/active/demo/SKILL.md",
    ).value["content"] == "supervisor-v2"
    assert store.get(
        ("public", "skills", "crawl-worker"),
        "/active/demo/SKILL.md",
    ).value["content"] == "crawl-worker-v1"


def test_public_seed_sync_does_not_delete_before_all_upserts(
    tmp_path,
    monkeypatch,
) -> None:
    for agent_name in ("supervisor", "data-analyst", "crawl-worker"):
        _write_seed(tmp_path, agent_name, "demo", agent_name)
    monkeypatch.setattr(mongodb_store, "SKILL_SEED_ROOT", tmp_path)

    class FailingStore(InMemoryStore):
        fail = False

        def put(self, namespace, key, value, *, index=None, ttl=None):
            if self.fail and namespace == ("public", "skills", "data-analyst"):
                raise RuntimeError("simulated upsert failure")
            return super().put(namespace, key, value, index=index, ttl=ttl)

    store = FailingStore()
    stale_namespace = ("public", "skills", "supervisor")
    store.put(stale_namespace, "/active/stale/SKILL.md", {"content": "stale"})
    store.fail = True

    with pytest.raises(RuntimeError, match="simulated upsert failure"):
        _sync_public_skills(store)
    assert store.get(stale_namespace, "/active/stale/SKILL.md") is not None
