---
name: procurement-analysis
description: 对公开网页中的采购报价进行需求澄清、供应商与价格采集、标准化分析、PNG 图表生成和证据化报告。用户要求采购分析、供应商比价、询价比较、采购推荐或市场价格调研时使用。
---

# 采购网页分析

严格按“理解需求 → 收集数据 → 执行分析 → 生成图表 → 生成报告”推进，使用
write_todos 记录跨轮进度。网页事实必须来自 crawl-worker，不得补写未采集的数据。

## 阶段一 — 理解需求

① 确认产品型号与规格、采购数量、交付地区、目标币种、价格时间范围和特殊约束。
② 缺少影响可比性的关键信息时调用 ask_user，一次最多请求三个字段；收到补充前停止推进，
不得自行假设规格、税率、运费或汇率。

## 阶段二 — 收集数据

③ 按产品或比较组启动 crawl-worker；每组最多两个并行任务。任务描述必须要求：
   - 优先制造商官网、授权渠道、供应商官网和可信电商；
   - 最多 6 次搜索、2 轮正文提取，取得 4–6 个有效来源后停止；
   - 返回供应商、网页标题、URL、采集时间、原始价格、币种、计价单位、价格类型、
     MOQ、运费税费、交期、库存及规格证据；超出预算时用已有证据返回部分结果。
④ 启动后立即向用户返回完整 task ID，不在同一轮轮询。下一轮先用 check_async_task
   获取最新状态；全部完成后再继续分析。任务失败时先说明错误摘要；内部类型错误、校验失败、
   配置错误或权限错误不得原样重启，只有明确的临时连接或超时故障才允许重试一次。
⑤ 把可核验报价写入 /workspace/output/procurement_quotes.csv。必须包含以下列：

```text
item,supplier,source_url,collected_at,currency,comparable_unit_cost,
spec_match_score,supplier_confidence_score,delivery_score,terms_score
```

可附加 listed_price、price_type、unit、quantity_basis、moq、lead_time_days、
shipping_tax_notes、notes。无法可靠换算时将 comparable_unit_cost 留空。

## 阶段三 — 执行分析

⑥ 统一币种、计价单位和采购数量口径，记录汇率来源与日期；不同币种或单位不得直接排名。
⑦ 评分均使用 0–100：规格完全匹配为 100、可接受替代为 70、部分匹配为 40；供应商
   官网或授权渠道为 100、成熟分销商为 80、已核验电商商家为 60；交付和条款按用户约束
   对应 100、70、40。没有证据时留空，不得猜分。
⑧ 默认总分权重为可比成本 40%、规格匹配 25%、供应商可信度 15%、交付能力 10%、
   MOQ/交易条款 10%。缺少任一评分项时只保留价格比较和定性评价，不计算总分。

## 阶段四 — 生成图表

⑨ 先检查 pandas 和 matplotlib；缺少时仅执行：

```text
python -m pip install --disable-pip-version-check --no-input \
  -i https://mirrors.aliyun.com/pypi/simple/ \
  -r /skills/public/supervisor/active/procurement-analysis/requirements.txt
```

⑩ 执行：

```text
python /skills/public/supervisor/active/procurement-analysis/scripts/analyze_quotes.py \
  --input /workspace/output/procurement_quotes.csv \
  --output-dir /workspace/output/charts \
  --summary /workspace/output/procurement_summary.json
```

读取 summary 判断是否可比较。至少两个同品项、同币种的有效报价才生成
price_comparison.png；至少两个完整评分才生成 supplier_score.png。不得为了出图填造数据。

## 阶段五 — 生成报告

⑪ 完整阅读 /skills/public/supervisor/active/evidence-reporting/SKILL.md，再写
/workspace/output/final_report.md。
报告包含执行摘要、需求与口径、报价表、评分与推荐、图表、风险和缺失数据、谈判建议及来源。
使用 `charts/price_comparison.png` 和 `charts/supplier_score.png` 相对路径引用实际存在的图表。
⑫ 默认将 Markdown 转换为 /workspace/output/final_report.pdf；转换失败时保留 Markdown 并
说明原因。最终回复先给 3–5 条结论，再列出 PDF、Markdown、CSV、JSON 和 PNG 路径。

## 注意事项

- 区分网页标价、促销价、批量报价和推算结果；网页标价不等同于可成交价格。
- 不把搜索摘要当作最终证据；规格、价格和供应商身份尽量回到原始页面核验。
- 只运行数据清洗、评分、制图和报告生成代码，不执行通用软件开发或系统管理操作。
- 单一供应商、关键价格缺失或口径不可比时，明确返回数据不足，不生成虚假排名。
