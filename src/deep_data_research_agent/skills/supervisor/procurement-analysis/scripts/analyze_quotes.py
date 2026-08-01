"""Validate normalized procurement quotes and generate deterministic charts."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import matplotlib
import pandas as pd

matplotlib.use("Agg")
from matplotlib import pyplot as plt

REQUIRED_COLUMNS = (
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
)
SCORE_COLUMNS = (
    "spec_match_score",
    "supplier_confidence_score",
    "delivery_score",
    "terms_score",
)
WEIGHTS = {
    "cost_score": 0.40,
    "spec_match_score": 0.25,
    "supplier_confidence_score": 0.15,
    "delivery_score": 0.10,
    "terms_score": 0.10,
}


def _load_quotes(input_path: Path) -> pd.DataFrame:
    """Load the stable CSV contract and normalize numeric fields."""

    frame = pd.read_csv(input_path, dtype=str, keep_default_na=False)
    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"采购报价 CSV 缺少必需列：{'、'.join(missing)}")

    for column in ("comparable_unit_cost", *SCORE_COLUMNS):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    for column in ("item", "supplier", "source_url", "currency"):
        frame[column] = frame[column].astype(str).str.strip()
    return frame


def _valid_group(
    item: str,
    group: pd.DataFrame,
    warnings: list[str],
) -> tuple[str, pd.DataFrame] | None:
    """Return one comparable item group without mixing currencies."""

    candidates = group[
        group["supplier"].ne("")
        & group["source_url"].str.startswith(("http://", "https://"))
        & group["currency"].ne("")
        & group["comparable_unit_cost"].gt(0)
    ].copy()
    currencies = sorted(candidates["currency"].str.upper().unique())
    if len(currencies) != 1:
        warnings.append(f"{item} 缺少统一币种的可比报价，已跳过排名")
        return None
    if len(candidates) < 2:
        warnings.append(f"{item} 少于两个有效报价，已跳过排名")
        return None

    currency = currencies[0]
    candidates["currency"] = currency
    minimum = float(candidates["comparable_unit_cost"].min())
    candidates["cost_score"] = minimum / candidates["comparable_unit_cost"] * 100
    return currency, candidates.sort_values("comparable_unit_cost")


def _score_group(
    item: str,
    group: pd.DataFrame,
    warnings: list[str],
) -> pd.DataFrame | None:
    """Calculate a total score only when every evidence-backed score exists."""

    complete = group.dropna(subset=list(SCORE_COLUMNS)).copy()
    in_range = complete[list(SCORE_COLUMNS)].ge(0).all(axis=1) & complete[
        list(SCORE_COLUMNS)
    ].le(100).all(axis=1)
    complete = complete[in_range]
    if len(complete) < 2:
        warnings.append(f"{item} 完整评分少于两个，不生成供应商总分")
        return None

    complete["total_score"] = sum(
        complete[column] * weight for column, weight in WEIGHTS.items()
    )
    return complete.sort_values("total_score", ascending=False)


def _plot_price(
    groups: list[tuple[str, str, pd.DataFrame]],
    output_path: Path,
) -> None:
    """Create one subplot per item so unrelated products are not compared."""

    figure, axes = plt.subplots(
        len(groups),
        1,
        figsize=(10, max(4, 4 * len(groups))),
        squeeze=False,
    )
    for axis, (item, currency, group) in zip(axes[:, 0], groups, strict=True):
        labels = group["supplier"].tolist()
        values = group["comparable_unit_cost"].astype(float).tolist()
        bars = axis.bar(labels, values, color="#247BA0")
        axis.set_title(f"{item} - Comparable Unit Cost")
        axis.set_ylabel(currency)
        axis.tick_params(axis="x", labelrotation=25)
        axis.bar_label(bars, fmt="%.2f", padding=3)
    figure.tight_layout()
    figure.savefig(output_path, dpi=160, bbox_inches="tight")
    plt.close(figure)


def _plot_scores(
    groups: list[tuple[str, pd.DataFrame]],
    output_path: Path,
) -> None:
    """Create evidence-backed supplier score charts."""

    figure, axes = plt.subplots(
        len(groups),
        1,
        figsize=(10, max(4, 4 * len(groups))),
        squeeze=False,
    )
    for axis, (item, group) in zip(axes[:, 0], groups, strict=True):
        labels = group["supplier"].tolist()
        values = group["total_score"].astype(float).tolist()
        bars = axis.bar(labels, values, color="#70C1B3")
        axis.set_title(f"{item} - Supplier Score")
        axis.set_ylabel("Score")
        axis.set_ylim(0, 105)
        axis.tick_params(axis="x", labelrotation=25)
        axis.bar_label(bars, fmt="%.1f", padding=3)
    figure.tight_layout()
    figure.savefig(output_path, dpi=160, bbox_inches="tight")
    plt.close(figure)


def analyze_quotes(
    input_path: Path,
    output_dir: Path,
    summary_path: Path,
) -> dict[str, Any]:
    """Analyze comparable quote groups and persist charts plus a JSON summary."""

    frame = _load_quotes(input_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    price_path = output_dir / "price_comparison.png"
    score_path = output_dir / "supplier_score.png"
    # Remove only this script's outputs so reruns cannot expose stale charts.
    price_path.unlink(missing_ok=True)
    score_path.unlink(missing_ok=True)

    warnings: list[str] = []
    price_groups: list[tuple[str, str, pd.DataFrame]] = []
    score_groups: list[tuple[str, pd.DataFrame]] = []
    rankings: list[dict[str, Any]] = []

    named_rows = frame[frame["item"].ne("")]
    if len(named_rows) != len(frame):
        warnings.append("存在未填写 item 的报价，已忽略")

    for item, raw_group in named_rows.groupby("item", sort=True):
        valid = _valid_group(str(item), raw_group, warnings)
        if valid is None:
            continue
        currency, price_group = valid
        price_groups.append((str(item), currency, price_group))
        score_group = _score_group(str(item), price_group, warnings)
        if score_group is not None:
            score_groups.append((str(item), score_group))
            scores = score_group["total_score"]
        else:
            scores = pd.Series(dtype=float)

        for index, row in price_group.iterrows():
            total_score = scores.get(index)
            rankings.append(
                {
                    "item": str(item),
                    "supplier": row["supplier"],
                    "source_url": row["source_url"],
                    "currency": currency,
                    "comparable_unit_cost": round(
                        float(row["comparable_unit_cost"]), 4
                    ),
                    "cost_score": round(float(row["cost_score"]), 2),
                    "total_score": (
                        round(float(total_score), 2)
                        if pd.notna(total_score)
                        else None
                    ),
                }
            )

    charts: list[str] = []
    if price_groups:
        _plot_price(price_groups, price_path)
        charts.append(price_path.as_posix())
    if score_groups:
        _plot_scores(score_groups, score_path)
        charts.append(score_path.as_posix())

    if price_groups and score_groups:
        status = "success"
    elif price_groups:
        status = "partial"
    else:
        status = "insufficient_data"

    summary: dict[str, Any] = {
        "status": status,
        "generated_at": datetime.now(UTC).isoformat(),
        "input_rows": len(frame),
        "weights": WEIGHTS,
        "comparable_groups": [item for item, _currency, _group in price_groups],
        "scored_groups": [item for item, _group in score_groups],
        "rankings": rankings,
        "charts": charts,
        "warnings": warnings,
    }
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="生成采购报价分析摘要和 PNG 图表")
    parser.add_argument("--input", required=True, type=Path, help="标准化报价 CSV")
    parser.add_argument("--output-dir", required=True, type=Path, help="PNG 输出目录")
    parser.add_argument("--summary", required=True, type=Path, help="JSON 摘要路径")
    args = parser.parse_args()

    try:
        result = analyze_quotes(args.input, args.output_dir, args.summary)
    except (OSError, ValueError) as exc:
        parser.exit(2, f"采购分析失败：{exc}\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
