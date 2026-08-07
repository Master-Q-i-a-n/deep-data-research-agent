"""Shared helpers for MongoDB-backed Skill files and seed packages."""

from __future__ import annotations

import base64
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SKILL_AGENT_NAMES = frozenset({"supervisor", "data-analyst", "crawl-worker"})
SKILL_SEED_ROOT = Path(__file__).resolve().parent / "skills"


def public_skill_namespace(agent_name: str) -> tuple[str, ...]:
    """Return the public Skill namespace owned by one Agent type."""

    return ("public", "skills", agent_name)


def public_skill_root(agent_name: str) -> str:
    """Return the virtual and physical root for one Agent's public Skills."""

    return f"/skills/public/{agent_name}/active"


def user_skill_root(agent_name: str) -> str:
    """Return the virtual and physical root for one Agent's user Skills."""

    return f"/skills/user/{agent_name}/active"


def file_store_value(
    content: bytes,
    *,
    created_at: str | None = None,
) -> dict[str, str]:
    """Encode one file using the StoreBackend v2 representation."""

    now = datetime.now(UTC).isoformat()
    try:
        text = content.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        text = base64.b64encode(content).decode("ascii")
        encoding = "base64"
    return {
        "content": text,
        "encoding": encoding,
        "sha256": hashlib.sha256(content).hexdigest(),
        "created_at": created_at or now,
        "modified_at": now,
    }


def stored_file_content(value: dict[str, Any]) -> bytes:
    """Decode one StoreBackend v2 file value."""

    content = value.get("content")
    encoding = value.get("encoding")
    if not isinstance(content, str):
        raise TypeError("MongoDB Skill 文件缺少字符串内容")
    if encoding == "base64":
        return base64.b64decode(content, validate=True)
    if encoding in {None, "utf-8"}:
        return content.encode("utf-8")
    raise ValueError(f"MongoDB Skill 文件编码不受支持：{encoding}")


def rewrite_candidate_content(
    content: bytes,
    *,
    skill_name: str,
    agent_name: str,
) -> bytes:
    """Resolve the candidate Skill root for one assignment target."""

    target = f"{user_skill_root(agent_name)}/{skill_name}".encode()
    replacements = (
        b"{{SKILL_ROOT}}",
        f"/persisted-skills/active/{skill_name}".encode(),
    )
    rewritten = content
    for source in replacements:
        rewritten = rewritten.replace(source, target)
    return rewritten
