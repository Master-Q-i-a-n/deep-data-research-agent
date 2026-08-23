"""Pull public Skill files from MongoDB and replace the repository Skill tree.

This script intentionally connects with ``pymongo`` directly.  Calling the
application's MongoDB store factory here would run its normal local-to-MongoDB
seed synchronization before the pull and could destroy the remote snapshot.
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
import uuid
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from pymongo import MongoClient
from pymongo.errors import PyMongoError

from deep_data_research_agent.core.config import get_settings
from deep_data_research_agent.skill_system.storage import (
    SKILL_AGENT_NAMES,
    SKILL_SEED_ROOT,
    public_skill_namespace,
    stored_file_content,
)

SkillSnapshot = dict[str, dict[PurePosixPath, bytes]]


def _relative_active_path(key: object) -> PurePosixPath:
    """Validate a MongoDB active-file key before using it as a local path."""

    if not isinstance(key, str) or not key.startswith("/active/"):
        raise ValueError(f"无效的 Skill 文件 key：{key!r}")
    raw_path = key.removeprefix("/active/")
    relative_path = PurePosixPath(raw_path)
    if (
        not raw_path
        or "\\" in raw_path
        or relative_path.is_absolute()
        or any(part in {"", ".", ".."} or ":" in part for part in relative_path.parts)
    ):
        raise ValueError(f"拒绝不安全的 Skill 文件路径：{key!r}")
    return relative_path


def load_public_skill_snapshot(collection: Any) -> SkillSnapshot:
    """Read and validate all public active Skill files from one collection."""

    snapshot: SkillSnapshot = {}
    for agent_name in sorted(SKILL_AGENT_NAMES):
        namespace = list(public_skill_namespace(agent_name))
        documents: Iterable[Mapping[str, Any]] = collection.find(
            {
                "namespace": namespace,
                "key": {"$regex": r"^/active/"},
            },
            {"_id": 0, "namespace": 1, "key": 1, "value": 1},
        )
        files: dict[PurePosixPath, bytes] = {}
        casefolded_paths: set[str] = set()
        for document in documents:
            if document.get("namespace") != namespace:
                raise ValueError(f"MongoDB 返回了非预期 namespace：{document.get('namespace')!r}")
            relative_path = _relative_active_path(document.get("key"))
            folded_path = relative_path.as_posix().casefold()
            if relative_path in files or folded_path in casefolded_paths:
                raise ValueError(f"发现重复或大小写冲突的 Skill 路径：{relative_path}")

            value = document.get("value")
            if not isinstance(value, dict):
                raise TypeError(f"Skill 文件 {relative_path} 的 value 不是对象")
            content = stored_file_content(value)
            expected_sha256 = value.get("sha256")
            actual_sha256 = hashlib.sha256(content).hexdigest()
            if expected_sha256 is not None and expected_sha256 != actual_sha256:
                raise ValueError(f"Skill 文件 {relative_path} 的 SHA-256 校验失败")

            files[relative_path] = content
            casefolded_paths.add(folded_path)

        if not files:
            raise ValueError(f"MongoDB 中没有 {agent_name} 的 public active Skill 文件")

        skill_names = {path.parts[0] for path in files}
        for skill_name in sorted(skill_names):
            if PurePosixPath(skill_name, "SKILL.md") not in files:
                raise ValueError(f"{agent_name}/{skill_name} 缺少 SKILL.md")
        snapshot[agent_name] = files

    return snapshot


def _write_snapshot(snapshot: SkillSnapshot, destination: Path) -> None:
    """Materialize a validated snapshot in a new staging directory."""

    for agent_name, files in snapshot.items():
        for relative_path, content in files.items():
            target = destination / agent_name / Path(*relative_path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)


def _replace_tree(snapshot: SkillSnapshot, target: Path) -> None:
    """Replace one tree, restoring the old tree if staging promotion fails."""

    target.parent.mkdir(parents=True, exist_ok=True)
    staging_parent = Path(tempfile.mkdtemp(prefix=".skills-sync-", dir=target.parent))
    staging_tree = staging_parent / "skills"
    backup = target.parent / f".skills-sync-backup-{uuid.uuid4().hex}"
    moved_existing = False
    try:
        _write_snapshot(snapshot, staging_tree)
        if target.exists():
            target.replace(backup)
            moved_existing = True
        try:
            staging_tree.replace(target)
        except BaseException:
            if moved_existing and backup.exists() and not target.exists():
                backup.replace(target)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if staging_parent.exists():
            shutil.rmtree(staging_parent)


def replace_local_skill_tree(snapshot: SkillSnapshot) -> None:
    """Replace only the repository Skill root after an exact-path guard."""

    target = SKILL_SEED_ROOT.resolve()
    expected_target = (Path(__file__).resolve().parents[1] / "skills").resolve()
    if target != expected_target:
        raise RuntimeError(f"拒绝覆盖非预期目录：{target}")
    _replace_tree(snapshot, target)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 MongoDB public Skill namespace 覆盖本地 src/deep_data_research_agent/skills。"
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--yes", action="store_true", help="确认校验后覆盖本地 Skill 目录")
    mode.add_argument("--dry-run", action="store_true", help="只读取和校验，不修改本地文件")
    return parser


def main() -> None:
    """Run the one-way MongoDB-to-local Skill synchronization."""

    args = _build_parser().parse_args()
    settings = get_settings()
    if not settings.mongodb_uri.strip():
        raise RuntimeError("MONGODB_URI 未配置，不能同步 Skill")

    try:
        with MongoClient(settings.mongodb_uri, serverSelectionTimeoutMS=10_000) as client:
            client.admin.command("ping")
            collection = client[settings.mongodb_database][settings.mongodb_skill_collection]
            snapshot = load_public_skill_snapshot(collection)
    except PyMongoError as exc:
        raise SystemExit("MongoDB 连接或读取失败，请确认服务已启动且 MONGODB_URI 可用。") from exc

    file_count = sum(len(files) for files in snapshot.values())
    skill_count = sum(len({path.parts[0] for path in files}) for files in snapshot.values())
    if args.dry_run:
        print(f"校验通过：{len(snapshot)} 个 Agent，{skill_count} 个 Skill，{file_count} 个文件；未修改本地目录。")
        return

    replace_local_skill_tree(snapshot)
    print(f"同步完成：{len(snapshot)} 个 Agent，{skill_count} 个 Skill，{file_count} 个文件。")
    print(f"本地目录：{SKILL_SEED_ROOT.resolve()}")


if __name__ == "__main__":
    main()
