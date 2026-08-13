from types import SimpleNamespace

import pytest

from deep_data_research_agent import reset_memory


class Collection:
    def __init__(self, count=0, active=None) -> None:
        self.count = count
        self.active = active
        self.queries: list[dict] = []

    def find_one(self, query):
        self.queries.append(query)
        return self.active

    def delete_many(self, query):
        self.queries.append(query)
        count = self.count
        self.count = 0
        return SimpleNamespace(deleted_count=count)


class Client:
    def __init__(self, collections) -> None:
        self.database = collections

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def __getitem__(self, _name):
        return self.database


def _settings():
    return SimpleNamespace(
        mongodb_uri="mongodb://example",
        mongodb_database="db",
        mongodb_skill_collection="skill_files",
        mongodb_memory_collection="memories",
        mongodb_memory_job_collection="memory_update_jobs",
    )


def test_reset_clears_only_whitelisted_memory_collections(monkeypatch, capsys) -> None:
    collections = {
        "user_preferences": Collection(2),
        "memories": Collection(3),
        "memory_update_jobs": Collection(4),
        "memory_worker_leases": Collection(1),
        "skill_files": Collection(99),
    }
    monkeypatch.setattr(reset_memory, "get_settings", _settings)
    monkeypatch.setattr(reset_memory, "MongoClient", lambda _uri: Client(collections))

    reset_memory.main()

    assert collections["user_preferences"].count == 0
    assert collections["memories"].count == 0
    assert collections["memory_update_jobs"].count == 0
    assert collections["memory_worker_leases"].count == 0
    assert collections["skill_files"].count == 99
    assert "长期记忆已清空" in capsys.readouterr().out


def test_reset_refuses_to_run_while_worker_lease_is_active(monkeypatch) -> None:
    collections = {
        "user_preferences": Collection(2),
        "memories": Collection(3),
        "memory_update_jobs": Collection(4),
        "memory_worker_leases": Collection(active={"_id": "memory-consumer"}),
    }
    monkeypatch.setattr(reset_memory, "get_settings", _settings)
    monkeypatch.setattr(reset_memory, "MongoClient", lambda _uri: Client(collections))

    with pytest.raises(RuntimeError, match="先停止应用"):
        reset_memory.main()
    assert collections["memories"].count == 3
