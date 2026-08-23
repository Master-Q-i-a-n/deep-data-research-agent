from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Any

import pytest

from deep_data_research_agent.skill_system.storage import (
    SKILL_AGENT_NAMES,
    file_store_value,
    public_skill_namespace,
)
from deep_data_research_agent.skill_system.sync import (
    _replace_tree,
    load_public_skill_snapshot,
)


class FakeCollection:
    """Small Mongo collection stand-in that applies the namespace filter."""

    def __init__(self, documents: list[dict[str, Any]]) -> None:
        self.documents = documents

    def find(self, query: dict[str, Any], projection: dict[str, int]) -> list[dict[str, Any]]:
        del projection
        namespace = query["namespace"]
        return [document for document in self.documents if document["namespace"] == namespace]


def _valid_documents() -> list[dict[str, Any]]:
    documents = []
    for agent_name in SKILL_AGENT_NAMES:
        documents.append(
            {
                "namespace": list(public_skill_namespace(agent_name)),
                "key": "/active/example/SKILL.md",
                "value": file_store_value(f"# {agent_name}\n".encode()),
            }
        )
    return documents


def test_load_public_skill_snapshot_decodes_all_agents() -> None:
    snapshot = load_public_skill_snapshot(FakeCollection(_valid_documents()))

    assert set(snapshot) == SKILL_AGENT_NAMES
    assert snapshot["supervisor"][PurePosixPath("example/SKILL.md")] == b"# supervisor\n"


def test_load_public_skill_snapshot_rejects_path_traversal() -> None:
    documents = _valid_documents()
    documents[0]["key"] = "/active/../outside/SKILL.md"

    with pytest.raises(ValueError, match="不安全"):
        load_public_skill_snapshot(FakeCollection(documents))


def test_load_public_skill_snapshot_rejects_bad_hash() -> None:
    documents = _valid_documents()
    documents[0]["value"]["sha256"] = "0" * 64

    with pytest.raises(ValueError, match="SHA-256"):
        load_public_skill_snapshot(FakeCollection(documents))


def test_replace_tree_removes_old_files(tmp_path: Path) -> None:
    target = tmp_path / "skills"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    snapshot = {
        "supervisor": {PurePosixPath("example/SKILL.md"): b"# new\n"},
    }

    _replace_tree(snapshot, target)

    assert not (target / "old.txt").exists()
    assert (target / "supervisor/example/SKILL.md").read_bytes() == b"# new\n"
