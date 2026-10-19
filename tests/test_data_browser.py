"""Data browsing validates structured filters without executing user SQL."""

import json
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException

from deep_data_research_agent.api import app as webapp
from deep_data_research_agent.api import data_browser as browser
from deep_data_research_agent.database import repository


def column(name, kind="text", type_name="text", primary_key=False):
    return {
        "name": name, "data_type": type_name, "type_name": type_name,
        "nullable": True, "primary_key": primary_key, "comment": None,
        "kind": kind, "label": name, "options": [],
    }


TABLE = {
    "name": "orders", "comment": "每行代表一笔订单。", "columns": [
        column("order_id", primary_key=True), column("order_status"),
        column("order_purchase_timestamp", "date", "timestamp"),
        column("price", "number", "numeric"),
    ],
}


def query(**kwargs):
    return browser.DataQuery(schema_name="olist", table_name="orders", **kwargs)


@pytest.fixture
def backend(monkeypatch):
    calls = []
    settings = SimpleNamespace(postgres_mcp_enabled=True, postgres_mcp_export_rows=2, postgres_mcp_export_bytes=4096)
    monkeypatch.setattr(browser, "get_settings", lambda: settings)

    async def resolve(token):
        return repository.UserRecord("user-a", "Alice", False) if token == "valid" else None

    async def invoke(tool_name, arguments, runtime):
        assert tool_name == "execute_sql" and runtime is None
        statement = arguments["sql"]
        calls.append(statement)
        if "AS total" in statement:
            rows = [{"total": 41, "rows": [{"order_id": "one", "order_status": "delivered"}]}]
        elif "AS columns" in statement:
            rows = [deepcopy(TABLE)]
        elif "NOT c.relispartition" in statement:
            rows = [{"schema_name": "olist", "name": "orders", "comment": "订单记录。"}]
        else:
            rows = [{"order_id": "=SUM(1,2)", "order_status": "delivered"}, {"order_id": "second", "order_status": "canceled"}]
        return repr([{"__bridge_json": json.dumps(rows)}])

    monkeypatch.setattr(repository, "resolve_login_session", resolve)
    monkeypatch.setattr(browser.mcp, "_invoke_text", invoke)
    return calls, settings


@pytest.mark.asyncio
async def test_catalog_details_and_pagination_share_readonly_channel(backend):
    calls, _ = backend
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url="http://test", headers={"Authorization": "Bearer valid"}) as client:
        catalog = await client.get("/data/catalog")
        assert catalog.json()["label"] == "Olist 电商数据库"
        assert catalog.json()["tables"][0]["label"] == "订单"
        assert catalog.headers["Cache-Control"] == "no-store"
        details = (await client.get("/data/table", params={"schema_name": "olist", "table_name": "orders"})).json()
        assert details["label"] == "订单"
        assert details["columns"][1]["options"][5] == {"value": "delivered", "label": "已送达"}
        result = await client.post("/data/query", json={"schema_name": "olist", "table_name": "orders", "page": 2})
        assert result.status_code == 200
        assert result.json()["total"] == 41
        assert result.json()["page"] == 2
        assert 'ORDER BY "t"."order_id" LIMIT 20 OFFSET 20' in calls[-1]
        assert "has_table_privilege" in calls[-2]


@pytest.mark.asyncio
@pytest.mark.parametrize("method,path,body", [
    ("GET", "/data/catalog", None), ("GET", "/data/table?schema_name=olist&table_name=orders", None),
    ("POST", "/data/query", {"schema_name": "olist", "table_name": "orders"}),
    ("POST", "/data/export", {"schema_name": "olist", "table_name": "orders"}),
])
@pytest.mark.parametrize("token", [None, "expired"])
async def test_all_endpoints_require_login(backend, method, path, body, token):
    calls, _ = backend
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url="http://test") as client:
        response = await client.request(method, path, json=body, headers={"Authorization": f"Bearer {token}"} if token else {})
    assert response.status_code == 401
    assert calls == []


@pytest.mark.asyncio
async def test_disabled_and_unavailable_source_are_actionable(backend, monkeypatch):
    calls, settings = backend
    settings.postgres_mcp_enabled = False
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url="http://test", headers={"Authorization": "Bearer valid"}) as client:
        response = await client.get("/data/catalog")
        assert response.status_code == 503 and "尚未启用" in response.json()["detail"]
        assert not calls
        settings.postgres_mcp_enabled = True

        async def fail(*args):
            raise browser.mcp.DatabaseToolError("postgresql://reader:secret@host/db")

        monkeypatch.setattr(browser.mcp, "_invoke_text", fail)
        response = await client.get("/data/catalog")
        assert response.status_code == 503 and "secret" not in response.text


@pytest.mark.asyncio
async def test_missing_or_unreadable_table_is_not_queried(backend, monkeypatch):
    calls, _ = backend

    async def empty(*args):
        return []

    monkeypatch.setattr(browser, "_rows", empty)
    with pytest.raises(HTTPException) as failure:
        await browser._table("pg_catalog", "pg_authid")
    assert failure.value.status_code == 404
    assert not calls


def test_sql_literals_are_escaped_and_field_names_are_checked():
    value = "x'; DROP TABLE orders; --"
    statement, _ = browser._select(query(filters=[{"column": "order_status", "operator": "eq", "value": value}]), TABLE)
    assert "'x''; DROP TABLE orders; --'" in statement
    with pytest.raises(HTTPException):
        browser._select(query(filters=[{"column": "order_status; DROP TABLE orders", "operator": "eq", "value": "x"}]), TABLE)
    with pytest.raises(HTTPException):
        browser._select(query(columns=["missing"]), TABLE)
    with pytest.raises(HTTPException):
        browser._select(query(columns=["price", "price"]), TABLE)


def test_dates_include_end_day_and_equality_covers_whole_day():
    statement, _ = browser._select(query(filters=[{"column": "order_purchase_timestamp", "operator": "between", "value": "2018-01-01", "end_value": "2018-03-31"}]), TABLE)
    assert '"t"."order_purchase_timestamp" < \'2018-04-01\'' in statement
    statement, _ = browser._select(query(filters=[{"column": "order_purchase_timestamp", "operator": "eq", "value": "2018-03-31"}]), TABLE)
    assert ">= '2018-03-31'" in statement and "< '2018-04-01'" in statement


@pytest.mark.parametrize("condition", [
    {"column": "price", "operator": "eq", "value": "NaN"},
    {"column": "price", "operator": "between", "value": "20", "end_value": "10"},
    {"column": "order_purchase_timestamp", "operator": "between", "value": "2018-04-01", "end_value": "2018-03-31"},
    {"column": "order_purchase_timestamp", "operator": "eq", "value": "not-a-date"},
    {"column": "price", "operator": "contains", "value": "20"},
    {"column": "order_status", "operator": "eq", "value": ""},
])
def test_invalid_filter_values_are_rejected(condition):
    with pytest.raises(HTTPException) as failure:
        browser._select(query(filters=[condition]), TABLE)
    assert failure.value.status_code == 422


def test_any_conditions_null_and_literal_substring():
    statement, _ = browser._select(query(match="any", filters=[
        {"column": "order_status", "operator": "contains", "value": "_%"},
        {"column": "order_purchase_timestamp", "operator": "is_null"},
    ]), TABLE)
    assert "strpos(" in statement and "'_%'" in statement
    assert " OR " in statement and '"t"."order_purchase_timestamp" IS NULL' in statement


@pytest.mark.asyncio
async def test_csv_exports_all_filtered_rows_and_selected_columns_with_bom(backend):
    calls, _ = backend
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url="http://test", headers={"Authorization": "Bearer valid"}) as client:
        response = await client.post("/data/export", json={"schema_name": "olist", "table_name": "orders", "columns": ["order_id"], "page": 2})
    assert response.status_code == 200
    assert response.content.startswith(b"\xef\xbb\xbf")
    assert "订单编号" in response.content.decode("utf-8-sig")
    assert "'=SUM(1,2)" in response.content.decode("utf-8-sig")
    assert "delivered" not in response.text
    assert "OFFSET" not in calls[-1]


@pytest.mark.asyncio
async def test_oversized_export_is_rejected_instead_of_silently_truncated(backend):
    _, settings = backend
    settings.postgres_mcp_export_rows = 1
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url="http://test", headers={"Authorization": "Bearer valid"}) as client:
        response = await client.post("/data/export", json={"schema_name": "olist", "table_name": "orders"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_arbitrary_sql_and_invalid_pagination_are_not_accepted(backend):
    calls, _ = backend
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=webapp.app), base_url="http://test", headers={"Authorization": "Bearer valid"}) as client:
        for extra in ({"sql": "SELECT 1"}, {"page": 0}, {"page_size": 200}):
            response = await client.post("/data/query", json={"schema_name": "olist", "table_name": "orders", **extra})
            assert response.status_code == 422
    assert not calls
