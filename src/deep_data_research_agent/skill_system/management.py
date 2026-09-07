"""Authenticated private Skill listing and removal from MongoDB."""

import re

from pymongo import AsyncMongoClient

from deep_data_research_agent.core.config import get_settings
from deep_data_research_agent.skill_system.storage import validated_skill_name

_client: AsyncMongoClient | None = None


def _collection():
    global _client
    settings = get_settings()
    if _client is None:
        if not settings.mongodb_uri.strip():
            raise RuntimeError("MONGODB_URI 未配置")
        _client = AsyncMongoClient(
            settings.mongodb_uri,
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
            socketTimeoutMS=5000,
        )
    return _client[settings.mongodb_database][settings.mongodb_skill_collection]


def _user_scope(identity_hash: str) -> dict:
    # 严格限定三段私有 namespace，兼容所有 Agent，不触及公共或其他存储目录。
    return {
        "namespace.0": identity_hash,
        "namespace.1": "skills",
        "namespace.2": {"$exists": True},
        "namespace.3": {"$exists": False},
    }


async def list_user_skills(identity_hash: str) -> list[dict]:
    grouped: dict[str, set[str]] = {}
    async for document in _collection().find(
        _user_scope(identity_hash),
        {"key": 1, "namespace": 1},
        max_time_ms=5000,
    ):
        key = str(document.get("key", ""))
        match = re.fullmatch(r"/active/([^/]+)/.+|/manifests/([^/]+)\.json", key)
        if match is None:
            continue
        name = match.group(1) or match.group(2)
        try:
            if validated_skill_name(name) != name:
                continue
        except ValueError:
            continue
        grouped.setdefault(name, set()).add(document["namespace"][2])
    return [
        {"name": name, "agents": sorted(agents)}
        for name, agents in sorted(grouped.items())
    ]


async def delete_user_skill(identity_hash: str, skill_name: str) -> int:
    name = validated_skill_name(skill_name)
    result = await _collection().delete_many(
        {
            **_user_scope(identity_hash),
            "$or": [
                {"key": {"$regex": rf"^/active/{re.escape(name)}/"}},
                {"key": f"/manifests/{name}.json"},
            ],
        }
    )
    return result.deleted_count


async def close_skill_management_client() -> None:
    global _client
    if _client is not None:
        await _client.close()
        _client = None
