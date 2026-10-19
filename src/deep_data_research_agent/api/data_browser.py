"""Authenticated, structured data browsing over the existing read-only MCP."""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse, Response
from psycopg import sql
from pydantic import BaseModel, ConfigDict, Field

from deep_data_research_agent.core.config import get_settings
from deep_data_research_agent.tools import database as mcp

router = APIRouter(prefix="/data", tags=["数据浏览"])

TABLE_LABELS = {
    "orders": "订单", "order_items": "商品明细", "customers": "客户",
    "products": "商品", "order_payments": "支付", "order_reviews": "评价",
    "sellers": "卖家", "geolocation": "地理位置", "category_translation": "品类对照",
    "marketing_qualified_leads": "招商线索", "closed_deals": "签约记录",
}
COLUMN_LABELS = {
    "order_id": "订单编号", "customer_id": "客户编号", "customer_unique_id": "消费者编号",
    "order_status": "订单状态", "order_purchase_timestamp": "下单时间",
    "order_approved_at": "支付确认时间", "order_delivered_carrier_date": "交付承运商时间",
    "order_delivered_customer_date": "实际送达时间", "order_estimated_delivery_date": "预计送达时间",
    "order_item_id": "商品序号", "product_id": "商品编号", "seller_id": "卖家编号",
    "shipping_limit_date": "发货截止时间", "price": "商品金额", "freight_value": "运费",
    "customer_zip_code_prefix": "客户邮编", "customer_city": "客户城市", "customer_state": "客户所在州",
    "seller_zip_code_prefix": "卖家邮编", "seller_city": "卖家城市", "seller_state": "卖家所在州",
    "payment_sequential": "支付序号", "payment_type": "支付方式", "payment_installments": "分期期数",
    "payment_value": "支付金额", "review_id": "评价编号", "review_score": "评分",
    "review_comment_title": "评价标题", "review_comment_message": "评价内容",
    "review_creation_date": "评价创建时间", "review_answer_timestamp": "评价回复时间",
    "product_category_name": "商品品类", "product_category_name_english": "品类英文名称",
    "product_name_lenght": "商品名称长度", "product_description_lenght": "商品描述长度",
    "product_photos_qty": "商品照片数量", "product_weight_g": "商品重量（克）",
    "product_length_cm": "商品长度（厘米）", "product_height_cm": "商品高度（厘米）",
    "product_width_cm": "商品宽度（厘米）", "geolocation_id": "位置编号",
    "geolocation_zip_code_prefix": "邮编", "geolocation_lat": "纬度", "geolocation_lng": "经度",
    "geolocation_city": "城市", "geolocation_state": "所在州", "mql_id": "线索编号",
    "first_contact_date": "首次联系日期", "landing_page_id": "落地页编号", "origin": "线索来源",
    "sdr_id": "销售开发人员编号", "sr_id": "销售代表编号", "won_date": "签约时间",
    "business_segment": "业务领域", "lead_type": "线索类型", "lead_behaviour_profile": "线索行为画像",
    "has_company": "是否有公司", "has_gtin": "是否有商品条码", "average_stock": "平均库存",
    "business_type": "业务类型", "declared_product_catalog_size": "申报商品数量",
    "declared_monthly_revenue": "申报月收入",
}
STATUS_OPTIONS = [
    {"value": value, "label": label} for value, label in [
        ("created", "已创建"), ("approved", "已确认"), ("invoiced", "已开票"),
        ("processing", "处理中"), ("shipped", "已发货"), ("delivered", "已送达"),
        ("unavailable", "不可用"), ("canceled", "已取消"),
    ]
]


class DataFilter(BaseModel):
    model_config = ConfigDict(extra="forbid")
    column: str = Field(min_length=1, max_length=128)
    operator: Literal["eq", "neq", "contains", "gt", "gte", "lt", "lte", "between", "is_null", "not_null"]
    value: str | None = Field(default=None, max_length=2000)
    end_value: str | None = Field(default=None, max_length=2000)


class DataQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_name: str = Field(min_length=1, max_length=128)
    table_name: str = Field(min_length=1, max_length=128)
    columns: list[str] = Field(default_factory=list, max_length=128)
    filters: list[DataFilter] = Field(default_factory=list, max_length=12)
    match: Literal["all", "any"] = "all"
    page: int = Field(default=1, ge=1, le=50_000)
    page_size: Literal[20, 50, 100] = 20


def _kind(type_name: str) -> str:
    if type_name in {"int2", "int4", "int8", "float4", "float8", "numeric"}:
        return "number"
    if type_name in {"date", "timestamp", "timestamptz"}:
        return "date"
    if type_name == "bool":
        return "boolean"
    if type_name in {"text", "varchar", "bpchar", "name", "uuid"}:
        return "text"
    return "other"


def _label(name: str, comment: str | None, index: int, *, table: bool = False) -> str:
    labels = TABLE_LABELS if table else COLUMN_LABELS
    if name in labels:
        return labels[name]
    if comment and re.search(r"[\u4e00-\u9fff]", comment):
        return re.split(r"[，。；\n]", comment.strip())[0][:24]
    # 未配置中文表名的表也不在界面暴露英文名称；真实标识仅用于 API。
    return f"数据表 {index + 1}" if table else name


async def _rows(query: str, limit: int) -> list[dict]:
    text = await mcp._invoke_text("execute_sql", {"sql": mcp._bridge_sql(query, limit)}, None)
    rows = mcp._parse_bridge_rows(text)
    if len(rows) > limit:
        raise HTTPException(422, "查询结果过多，请缩小范围")
    return rows


async def _authorize(authorization: str | None) -> None:
    # 局部导入避免 app 注册路由时的循环依赖，复用现有登录失效处理。
    from deep_data_research_agent.api.app import _authenticated_user_id

    await _authenticated_user_id(authorization)
    if not get_settings().postgres_mcp_enabled:
        raise HTTPException(503, "数据库数据源尚未启用，请联系管理员配置只读数据源")


async def _table(schema_name: str, table_name: str) -> dict:
    schema_value = sql.Literal(schema_name).as_string()
    table_value = sql.Literal(table_name).as_string()
    rows = await _rows(f"""
SELECT c.relname AS name, td.description AS comment,
 COALESCE((SELECT jsonb_agg(jsonb_build_object(
  'name', a.attname, 'data_type', format_type(a.atttypid, a.atttypmod),
  'type_name', t.typname, 'nullable', NOT a.attnotnull,
  'comment', cd.description,
  'primary_key', EXISTS(SELECT 1 FROM pg_index i WHERE i.indrelid=c.oid
    AND i.indisprimary AND a.attnum=ANY(i.indkey))
 ) ORDER BY a.attnum) FROM pg_attribute a JOIN pg_type t ON t.oid=a.atttypid
 LEFT JOIN pg_description cd ON cd.objoid=a.attrelid AND cd.objsubid=a.attnum
  AND cd.classoid='pg_catalog.pg_class'::regclass
 WHERE a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped), '[]'::jsonb) AS columns
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
LEFT JOIN pg_description td ON td.objoid=c.oid AND td.objsubid=0
 AND td.classoid='pg_catalog.pg_class'::regclass
WHERE n.nspname={schema_value} AND c.relname={table_value} AND c.relkind IN ('r','p')
 AND left(n.nspname,3)<>'pg_' AND n.nspname<>'information_schema'
 AND has_schema_privilege(n.oid,'USAGE') AND has_table_privilege(c.oid,'SELECT')
""", 1)
    if not rows:
        raise HTTPException(404, "数据表不存在或没有读取权限")
    result = rows[0]
    result["label"] = _label(table_name, result["comment"], 0, table=True)
    for index, column in enumerate(result["columns"]):
        column["label"] = _label(column["name"], column["comment"], index)
        column["kind"] = _kind(column["type_name"])
        column["options"] = STATUS_OPTIONS if schema_name == "olist" and table_name == "orders" and column["name"] == "order_status" else []
    return result


def _value(value: str | None, column: dict) -> str:
    if value is None or not value.strip():
        raise HTTPException(422, "请完整填写筛选值")
    kind = column["kind"]
    value = value.strip()
    try:
        if kind == "number":
            number = Decimal(value)
            if not number.is_finite():
                raise ValueError
            return sql.Literal(number).as_string()
        if kind == "boolean":
            if value not in {"true", "false"}:
                raise ValueError
            return "TRUE" if value == "true" else "FALSE"
        if kind == "date":
            datetime.fromisoformat(value)
            if column["type_name"] == "date":
                date.fromisoformat(value)
        if column["type_name"] == "uuid":
            UUID(value)
    except (ValueError, InvalidOperation) as exc:
        raise HTTPException(422, f"「{column['label']}」的筛选值格式不正确") from exc
    return sql.Literal(value).as_string()


def _select(payload: DataQuery, table: dict) -> tuple[str, list[dict]]:
    available = {column["name"]: column for column in table["columns"]}
    names = payload.columns or list(available)
    if not names or len(names) != len(set(names)) or any(name not in available for name in names):
        raise HTTPException(422, "请选择有效且不重复的显示字段")
    columns = [available[name] for name in names]
    clauses = []
    for condition in payload.filters:
        column = available.get(condition.column)
        if column is None:
            raise HTTPException(422, "筛选字段不存在，请刷新字段说明")
        identifier = sql.Identifier("t", condition.column).as_string()
        operator = condition.operator
        if operator in {"is_null", "not_null"}:
            clauses.append(f"{identifier} IS {'NOT ' if operator == 'not_null' else ''}NULL")
            continue
        kind = column["kind"]
        allowed = {"eq", "neq"} if kind in {"text", "boolean"} else set()
        if kind == "text":
            allowed.add("contains")
        if kind in {"number", "date"}:
            allowed = {"eq", "neq", "gt", "gte", "lt", "lte", "between"}
        if operator not in allowed:
            raise HTTPException(422, f"「{column['label']}」不支持此筛选方式")
        value = _value(condition.value, column)
        if operator == "contains":
            # 使用 strpos 避免将用户输入中的 %、_ 解释为通配符。
            clauses.append(f"strpos(lower({identifier}::text), lower({value})) > 0")
        elif operator == "between":
            end = _value(condition.end_value, column)
            start_value = condition.value.strip()
            end_value = condition.end_value.strip()
            if kind == "number" and Decimal(start_value) > Decimal(end_value):
                raise HTTPException(422, "范围起始值不能大于结束值")
            if kind == "date":
                try:
                    reversed_range = datetime.fromisoformat(start_value) > datetime.fromisoformat(end_value)
                except TypeError as exc:
                    raise HTTPException(422, "范围内的两个日期必须使用相同的时区格式") from exc
                if reversed_range:
                    raise HTTPException(422, "开始日期不能晚于结束日期")
                if column["type_name"] != "date" and len(end_value) == 10:
                    # 日期选择器的结束日期包含整天，不能截断到当日零点。
                    try:
                        next_day = (date.fromisoformat(end_value) + timedelta(days=1)).isoformat()
                    except OverflowError as exc:
                        raise HTTPException(422, "结束日期超出支持范围") from exc
                    end = sql.Literal(next_day).as_string()
                    clauses.append(f"({identifier} >= {value} AND {identifier} < {end})")
                    continue
            clauses.append(f"{identifier} BETWEEN {value} AND {end}")
        else:
            if kind == "date" and column["type_name"] != "date" and len(condition.value.strip()) == 10:
                # 时间戳按日筛选时，“等于”覆盖整天，“大于”从次日开始。
                try:
                    next_day = (date.fromisoformat(condition.value.strip()) + timedelta(days=1)).isoformat()
                except OverflowError as exc:
                    raise HTTPException(422, "日期超出支持范围") from exc
                end = sql.Literal(next_day).as_string()
                if operator in {"eq", "neq"}:
                    clause = f"({identifier} >= {value} AND {identifier} < {end})"
                    clauses.append(f"NOT {clause}" if operator == "neq" else clause)
                else:
                    comparison = {"gt": ">=", "gte": ">=", "lt": "<", "lte": "<"}[operator]
                    clauses.append(f"{identifier} {comparison} {end if operator in {'gt', 'lte'} else value}")
                continue
            comparison = {"eq": "=", "neq": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}[operator]
            clauses.append(f"{identifier} {comparison} {value}")
    joiner = " AND " if payload.match == "all" else " OR "
    where = " WHERE " + joiner.join(f"({clause})" for clause in clauses) if clauses else ""
    relation = sql.Identifier(payload.schema_name, payload.table_name).as_string()
    selection = ", ".join(sql.Identifier("t", name).as_string() for name in names)
    return f"SELECT {selection} FROM {relation} AS t{where}", columns


def _order(table: dict) -> str:
    keys = [sql.Identifier("t", c["name"]).as_string() for c in table["columns"] if c["primary_key"]]
    # 无主键的实体表按物理定位排序；分区表同时用 tableoid 区分分区。
    return " ORDER BY " + (", ".join(keys) if keys else "t.tableoid, t.ctid")


@router.get("/catalog")
async def catalog(authorization: str | None = Header(default=None)) -> JSONResponse:
    await _authorize(authorization)
    try:
        tables = await _rows("""
SELECT n.nspname AS schema_name, c.relname AS name,
 d.description AS comment
FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
LEFT JOIN pg_description d ON d.objoid=c.oid AND d.objsubid=0
 AND d.classoid='pg_catalog.pg_class'::regclass
WHERE c.relkind IN ('r','p') AND NOT c.relispartition
 AND left(n.nspname,3)<>'pg_' AND n.nspname<>'information_schema'
 AND has_schema_privilege(n.oid,'USAGE') AND has_table_privilege(c.oid,'SELECT')
ORDER BY n.nspname,c.relname
""", 1000)
        for index, table in enumerate(tables):
            table["label"] = _label(table["name"], table["comment"], index, table=True)
        label = "Olist 电商数据库" if tables and all(t["schema_name"] == "olist" for t in tables) else "数据库数据源"
        return JSONResponse({"label": label, "tables": tables}, headers={"Cache-Control": "no-store"})
    except mcp.DatabaseToolError as exc:
        raise HTTPException(503, "无法读取数据库，请检查只读数据源连接后重试") from exc


@router.get("/table")
async def table_details(schema_name: str, table_name: str, authorization: str | None = Header(default=None)) -> JSONResponse:
    await _authorize(authorization)
    try:
        return JSONResponse(await _table(schema_name, table_name), headers={"Cache-Control": "no-store"})
    except mcp.DatabaseToolError as exc:
        raise HTTPException(503, "无法读取字段说明，请稍后重试") from exc


@router.post("/query")
async def query(payload: DataQuery, authorization: str | None = Header(default=None)) -> JSONResponse:
    await _authorize(authorization)
    try:
        table = await _table(payload.schema_name, payload.table_name)
        selection, columns = _select(payload, table)
        offset = (payload.page - 1) * payload.page_size
        # 总数与本页在同一 SQL 快照中读取，避免两个请求之间增删导致结果不一致。
        result = (await _rows(f"""
SELECT (SELECT count(*) FROM ({selection}) AS counted) AS total,
 COALESCE((SELECT jsonb_agg(to_jsonb(result_row)) FROM
 ({selection}{_order(table)} LIMIT {payload.page_size} OFFSET {offset}) AS result_row), '[]'::jsonb) AS rows
""", 1))[0]
        result.update(columns=columns, page=payload.page, page_size=payload.page_size)
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > get_settings().postgres_mcp_export_bytes:
            raise HTTPException(422, "本页数据过大，请减少显示字段或每页行数")
        return JSONResponse(result, headers={"Cache-Control": "no-store"})
    except mcp.DatabaseToolError as exc:
        raise HTTPException(503, "查询失败，请检查数据源连接或缩小查询范围后重试") from exc


@router.post("/export")
async def export(payload: DataQuery, authorization: str | None = Header(default=None)) -> Response:
    await _authorize(authorization)
    try:
        table = await _table(payload.schema_name, payload.table_name)
        selection, columns = _select(payload, table)
        settings = get_settings()
        rows = await _rows(f"{selection}{_order(table)} LIMIT {settings.postgres_mcp_export_rows + 1}", settings.postgres_mcp_export_rows)
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)

        def cell(value: object) -> object:
            if isinstance(value, (dict, list)):
                value = json.dumps(value, ensure_ascii=False)
            # 导出的文本不能被 Excel 当作公式执行；真实数值保留数值形式。
            if isinstance(value, str) and (value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@"))):
                return "'" + value
            return value

        writer.writerow([cell(c["label"]) for c in columns])
        writer.writerows([cell(row.get(c["name"])) for c in columns] for row in rows)
        content = stream.getvalue().encode("utf-8-sig")
        if len(content) > settings.postgres_mcp_export_bytes:
            raise HTTPException(422, "导出数据过大，请缩小筛选范围")
        return Response(content, media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": 'attachment; filename="query-results.csv"',
            "Cache-Control": "no-store",
        })
    except mcp.DatabaseToolError as exc:
        raise HTTPException(503, "导出失败，请检查数据源连接或缩小查询范围后重试") from exc
