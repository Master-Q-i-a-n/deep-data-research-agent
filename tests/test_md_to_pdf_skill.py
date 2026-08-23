"""内置 Markdown 转 PDF Skill 的发现与依赖回归测试。"""

from pathlib import Path

from deep_data_research_agent.agents.prompts import SUPERVISOR_PROMPT
from deep_data_research_agent.infrastructure.mongodb.store import _public_seed_values

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = (
    PROJECT_ROOT
    / "src"
    / "deep_data_research_agent"
    / "skills"
    / "supervisor"
    / "md-to-pdf"
)


def test_md_to_pdf_is_supervisor_public_seed() -> None:
    text = (SKILL_ROOT / "SKILL.md").read_text("utf-8")
    files = set(_public_seed_values("supervisor"))

    assert len(text.splitlines()) <= 100
    assert "name: md-to-pdf" in text
    assert "/skills/public/supervisor/active/md-to-pdf/scripts/md_to_pdf.py" in text
    assert {
        "/active/md-to-pdf/SKILL.md",
        "/active/md-to-pdf/scripts/md_to_pdf.py",
        "/manifests/md-to-pdf.json",
    } <= files
    assert "md-to-pdf" not in SUPERVISOR_PROMPT


def test_md_to_pdf_script_uses_lightweight_weasyprint_pipeline() -> None:
    script = (SKILL_ROOT / "scripts" / "md_to_pdf.py").read_text("utf-8")

    assert "from weasyprint import HTML" in script
    assert "import markdown" in script
    assert "import playwright" not in script.lower()
    assert "subprocess" not in script.lower()
    assert "pandoc" not in script.lower()
    # --no-mermaid/--no-math 仅用于兼容旧调用方，不会加载对应引擎。
    assert 'add_argument("--no-mermaid"' in script
