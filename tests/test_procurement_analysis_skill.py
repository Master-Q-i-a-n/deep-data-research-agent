"""公用采购分析 Skill 的发现、数据校验和制图回归测试。"""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from deep_data_research_agent.mongodb_store import _public_seed_values
from deep_data_research_agent.prompts import SUPERVISOR_PROMPT

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = (
    PROJECT_ROOT
    / "src"
    / "deep_data_research_agent"
    / "skills"
    / "supervisor"
    / "procurement-analysis"
)
SCRIPT_PATH = SKILL_ROOT / "scripts" / "analyze_quotes.py"
CSV_COLUMNS = [
    "item",
    "supplier",
    "source_url",
    "collected_at",
    "currency",
    "comparable_unit_cost",
    "spec_match_score",
    "supplier_confidence_score",
    "delivery_score",
    "terms_score",
]


def _quote(
    supplier: str,
    *,
    currency: str = "CNY",
    cost: str = "100",
) -> dict[str, str]:
    return {
        "item": "Widget-A",
        "supplier": supplier,
        "source_url": f"https://example.com/{supplier.lower()}",
        "collected_at": "2026-08-01T12:00:00+08:00",
        "currency": currency,
        "comparable_unit_cost": cost,
        "spec_match_score": "100",
        "supplier_confidence_score": "80",
        "delivery_score": "70",
        "terms_score": "70",
    }


def _run_analysis(tmp_path: Path, rows: list[dict[str, str]]) -> dict:
    input_path = tmp_path / "quotes.csv"
    output_dir = tmp_path / "charts"
    summary_path = tmp_path / "summary.json"
    with input_path.open("w", encoding="utf-8", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--input",
            str(input_path),
            "--output-dir",
            str(output_dir),
            "--summary",
            str(summary_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 0, result.stderr
    return json.loads(summary_path.read_text("utf-8"))


def test_procurement_skill_is_builtin_and_step_based() -> None:
    text = (SKILL_ROOT / "SKILL.md").read_text("utf-8")
    files = set(_public_seed_values("supervisor"))

    assert "name: procurement-analysis" in text
    assert "采购分析、供应商比价、询价比较、采购推荐或市场价格调研" in text
    for stage in ("理解需求", "收集数据", "执行分析", "生成图表", "生成报告"):
        assert stage in text
    assert {
        "/active/procurement-analysis/SKILL.md",
        "/active/procurement-analysis/requirements.txt",
        "/active/procurement-analysis/scripts/analyze_quotes.py",
    } <= files


def test_procurement_dependencies_and_prompt_are_restricted() -> None:
    requirements = (SKILL_ROOT / "requirements.txt").read_text("utf-8").splitlines()

    assert requirements == ["pandas>=3,<4", "matplotlib>=3.11,<4"]
    assert "采购" not in SUPERVISOR_PROMPT


def test_analysis_generates_price_and_score_charts(tmp_path: Path) -> None:
    summary = _run_analysis(
        tmp_path,
        [
            _quote("Alpha", cost="100"),
            _quote("Beta", cost="125"),
        ],
    )

    assert summary["status"] == "success"
    assert summary["comparable_groups"] == ["Widget-A"]
    assert summary["scored_groups"] == ["Widget-A"]
    assert len(summary["rankings"]) == 2
    assert all(row["total_score"] is not None for row in summary["rankings"])
    for filename in ("price_comparison.png", "supplier_score.png"):
        path = tmp_path / "charts" / filename
        assert path.is_file()
        assert path.stat().st_size > 0


@pytest.mark.parametrize(
    "rows",
    [
        [_quote("Alpha")],
        [_quote("Alpha", currency="CNY"), _quote("Beta", currency="USD")],
        [_quote("Alpha", cost=""), _quote("Beta", cost="")],
    ],
    ids=["single-supplier", "mixed-currency", "missing-price"],
)
def test_analysis_does_not_rank_incomparable_quotes(
    tmp_path: Path,
    rows: list[dict[str, str]],
) -> None:
    summary = _run_analysis(tmp_path, rows)

    assert summary["status"] == "insufficient_data"
    assert summary["rankings"] == []
    assert summary["charts"] == []
    assert not (tmp_path / "charts" / "price_comparison.png").exists()
    assert not (tmp_path / "charts" / "supplier_score.png").exists()


def test_analysis_omits_total_score_when_evidence_is_incomplete(
    tmp_path: Path,
) -> None:
    first = _quote("Alpha", cost="100")
    second = _quote("Beta", cost="125")
    second["delivery_score"] = ""

    summary = _run_analysis(tmp_path, [first, second])

    assert summary["status"] == "partial"
    assert all(row["total_score"] is None for row in summary["rankings"])
    assert (tmp_path / "charts" / "price_comparison.png").is_file()
    assert not (tmp_path / "charts" / "supplier_score.png").exists()
