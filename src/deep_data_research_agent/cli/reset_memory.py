"""One-time destructive reset for long-term memory collections only."""

from __future__ import annotations

from datetime import UTC, datetime

from pymongo import MongoClient

from deep_data_research_agent.core.config import get_settings


def main() -> None:
    """Clear only memory files, jobs, and worker leases after workers stop."""

    settings = get_settings()
    if not settings.mongodb_uri.strip():
        raise RuntimeError("MONGODB_URI 未配置，不能重置长期记忆")

    protected = {settings.mongodb_skill_collection}
    targets = {
        "user_preferences",  # 旧版用户偏好 collection
        settings.mongodb_memory_collection,
        settings.mongodb_memory_job_collection,
        "memory_worker_leases",
    }
    overlap = protected & targets
    if overlap:
        raise RuntimeError(f"拒绝清理受保护的 Skill collection：{sorted(overlap)}")

    with MongoClient(settings.mongodb_uri) as client:
        database = client[settings.mongodb_database]
        leases = database["memory_worker_leases"]
        active = leases.find_one(
            {
                "_id": {"$in": ["memory-consumer", "memory-experience-consumer"]},
                "lease_until": {"$gt": datetime.now(UTC)},
            }
        )
        if active is not None:
            raise RuntimeError("记忆 worker 仍持有有效租约，请先停止应用再执行重置")

        counts: dict[str, int] = {}
        for collection_name in sorted(targets - {"memory_worker_leases"}):
            counts[collection_name] = database[collection_name].delete_many({}).deleted_count
        counts["memory_worker_leases"] = leases.delete_many(
            {"_id": {"$in": ["memory-consumer", "memory-experience-consumer"]}}
        ).deleted_count

    print("长期记忆已清空：")
    for collection_name, count in counts.items():
        print(f"- {collection_name}: {count} documents")


if __name__ == "__main__":
    main()
