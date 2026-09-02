"""内置 Deep Research Skill 的发现、路由边界和项目适配回归测试。"""

from __future__ import annotations

from pathlib import Path

import yaml

from deep_data_research_agent.agents.prompts import SUPERVISOR_PROMPT
from deep_data_research_agent.infrastructure.mongodb.store import _public_seed_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = (
    PROJECT_ROOT
    / "src"
    / "deep_data_research_agent"
    / "skills"
    / "supervisor"
    / "deep-research"
)
SKILL_PATH = SKILL_ROOT / "SKILL.md"
EVIDENCE_REPORTING_PATH = (
    SKILL_ROOT.parent / "evidence-reporting" / "SKILL.md"
)


def _skill_frontmatter(text: str) -> dict[str, object]:
    """Parse the repository Skill frontmatter with the same YAML semantics."""

    assert text.startswith("---\n")
    _, frontmatter, _ = text.split("---", 2)
    parsed = yaml.safe_load(frontmatter)
    assert isinstance(parsed, dict)
    return parsed


def test_deep_research_is_a_public_supervisor_skill() -> None:
    text = SKILL_PATH.read_text("utf-8")
    files = set(_public_seed_values("supervisor"))
    metadata = _skill_frontmatter(text)

    assert metadata["name"] == "deep-research"
    assert "/active/deep-research/SKILL.md" in files
    assert len(text.splitlines()) <= 100
    # Skill discovery remains metadata-driven instead of expanding the base prompt.
    assert "deep-research" not in SUPERVISOR_PROMPT


def test_deep_research_description_discriminates_simple_searches() -> None:
    text = SKILL_PATH.read_text("utf-8")
    description = str(_skill_frontmatter(text)["description"])

    for positive in ("深度研究", "系统调研", "全面调研", "至少两个复杂信号"):
        assert positive in description
    for excluded in ("单次搜索", "单一事实", "最新状态", "单页摘要", "少量指定 URL"):
        assert excluded in description
    # Concrete examples make the model-facing boundary observable and reviewable.
    assert "搜索某产品今天的价格或最新消息" in text
    assert "总结这个 URL" in text
    assert "普通采购比价优先使用 `procurement-analysis`" in text


def test_deep_research_uses_project_tools_and_paths() -> None:
    text = SKILL_PATH.read_text("utf-8")

    for required in (
        "`write_todos`",
        "`ask_user`",
        "`web_search`",
        "`start_async_task`",
        "`check_async_task`",
        "`update_async_task`",
        "`cancel_async_task`",
        "/workspace/research/evidence_ledger.md",
        "/workspace/output/final_report.md",
    ):
        assert required in text
    for codex_only in (
        "`update_plan`",
        "`request_user_input`",
        "Work conversation",
        "collaboration tools",
        "artifact attachment",
    ):
        assert codex_only not in text
    assert "不因本 Skill 自动调用 Reviewer" in text
    assert "不得假定其路径可由 Supervisor 或 data-analyst 直接读取" in text


def test_evidence_reporting_defers_deep_research_default_format() -> None:
    text = EVIDENCE_REPORTING_PATH.read_text("utf-8")

    assert "当前任务使用 `deep-research` 时" in text
    assert "未指定格式只生成" in text
    assert "明确要求 PDF 时才读取 `md-to-pdf`" in text
