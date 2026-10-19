import { CSSProperties, FormEvent, useEffect, useMemo, useRef, useState } from "react";
import ColumnResizeHandle from "../layout/ColumnResizeHandle";
import "./data-explorer.css";

type Table = { schema_name: string; name: string; label: string; comment: string | null };
type Column = {
  name: string; label: string; comment: string | null; data_type: string;
  kind: "text" | "number" | "date" | "boolean" | "other";
  nullable: boolean; primary_key: boolean; options: { value: string; label: string }[];
};
type Details = { label: string; comment: string | null; columns: Column[] };
type Operator = "eq" | "neq" | "contains" | "gt" | "gte" | "lt" | "lte" | "between" | "is_null" | "not_null";
type Filter = { id: string; column: string; operator: Operator; value: string; end_value: string };
type Query = {
  schema_name: string; table_name: string; filters: Omit<Filter, "id">[];
  match: "all" | "any"; page: number; page_size: 20 | 50 | 100;
};
type Result = { rows: Record<string, unknown>[]; columns: Column[]; total: number; page: number; page_size: number };
type Props = { apiUrl: string; token: string; hidden: boolean; onExpired: () => void };

const OPERATORS: Record<Operator, string> = {
  eq: "等于", neq: "不等于", contains: "包含", gt: "大于", gte: "大于等于",
  lt: "小于", lte: "小于等于", between: "介于", is_null: "为空", not_null: "不为空",
};

const ICON_PATHS = {
  database: "M20 5c0 1.7-3.6 3-8 3S4 6.7 4 5s3.6-3 8-3 8 1.3 8 3ZM4 5v14c0 1.7 3.6 3 8 3s8-1.3 8-3V5M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  table: "M3 4h18v16H3ZM3 10h18M9 4v16M15 10v10",
  search: "M10.5 3a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15ZM16 16l5 5",
  refresh: "M20 7a8 8 0 1 0 1 8M20 3v5h-5",
  filter: "M3 5h18l-7 8v6l-4 2v-8Z",
  plus: "M12 5v14M5 12h14",
  reset: "M4 10a8 8 0 1 1 0 5M4 4v6h6M12 7v5l3 2",
  columns: "M3 4h18v16H3ZM9 4v16M15 4v16",
  download: "M12 3v12M7 10l5 5 5-5M4 16v5h16v-5",
  close: "M6 6l12 12M18 6 6 18",
  left: "M15 6l-6 6 6 6",
  right: "M9 6l6 6-6 6",
  orders: "M8 4H5v17h14V4h-3M8 2h8v4H8ZM8 11h8M8 15h5",
  customers: "M15 7a3 3 0 1 0-6 0 3 3 0 0 0 6 0ZM5 21v-3a7 7 0 0 1 14 0v3M19 4a3 3 0 0 1 0 6M21 14a5 5 0 0 1 2 4v3",
  products: "M3 7l9-5 9 5v10l-9 5-9-5ZM3 7l9 5 9-5M12 12v10M7 4l10 6",
  sellers: "M3 10l2-7h14l2 7M3 10a3 3 0 0 0 6 0 3 3 0 0 0 6 0 3 3 0 0 0 6 0M5 13v8h14v-8M9 21v-6h6v6",
  payments: "M3 5h18v14H3ZM3 10h18M7 15h3",
  reviews: "M21 12a8 8 0 0 1-8 8H4l-2 2V12a8 8 0 0 1 8-8h3a8 8 0 0 1 8 8ZM7 10h10M7 14h7",
  location: "M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 0 1 16 0ZM15 10a3 3 0 1 0-6 0 3 3 0 0 0 6 0Z",
  leads: "M21 12a9 9 0 1 1-9-9M17 12a5 5 0 1 1-5-5M12 12l9-9M17 3h4v4",
  deals: "M6 2h9l4 4v16H6ZM14 2v5h5M9 14l2 2 5-5",
  categories: "M3 3h7v7H3ZM14 3h7v7h-7ZM3 14h7v7H3ZM14 14h7v7h-7",
  fields: "M5 3h14v18H5ZM8 7h8M8 12h8M8 17h5",
};
const TABLE_ICONS: Record<string, keyof typeof ICON_PATHS> = {
  orders: "orders", order_items: "table", customers: "customers", products: "products",
  sellers: "sellers", order_payments: "payments", order_reviews: "reviews",
  geolocation: "location", marketing_qualified_leads: "leads", closed_deals: "deals", category_translation: "categories",
};

function DataIcon({ name }: { name: keyof typeof ICON_PATHS }) {
  return <svg className="data-icon" aria-hidden="true" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round"><path d={ICON_PATHS[name]} /></svg>;
}

function operators(column?: Column): Operator[] {
  const values: Operator[] = column?.kind === "number" || column?.kind === "date"
    ? ["eq", "neq", "gt", "gte", "lt", "lte", "between"]
    : column?.kind === "text" ? ["eq", "neq", "contains"]
      : column?.kind === "boolean" ? ["eq", "neq"] : [];
  return [...values, "is_null", "not_null"];
}

function displayValue(value: unknown, column: Column): string {
  if (value === null || value === undefined) return "—";
  const option = column.options.find(item => item.value === String(value));
  if (option) return option.label;
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "object") return JSON.stringify(value);
  // 保留数据库时间本身，不用浏览器时区悄悄转换历史订单时间。
  return column.kind === "date" ? String(value).replace("T", " ") : String(value);
}

export default function DataExplorer({ apiUrl, token, hidden, onExpired }: Props) {
  const [tableListWidth, setTableListWidth] = useState<number>();
  const [tables, setTables] = useState<Table[]>([]);
  const [sourceLabel, setSourceLabel] = useState("数据库数据源");
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [catalogError, setCatalogError] = useState("");
  const [revision, setRevision] = useState(0);
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Table | null>(null);
  const [details, setDetails] = useState<Details | null>(null);
  const [detailsLoading, setDetailsLoading] = useState(false);
  const [detailsError, setDetailsError] = useState("");
  const [tab, setTab] = useState<"preview" | "fields">("preview");
  const [filters, setFilters] = useState<Filter[]>([]);
  const [match, setMatch] = useState<"all" | "any">("all");
  const [query, setQuery] = useState<Query | null>(null);
  const [result, setResult] = useState<Result | null>(null);
  const [queryLoading, setQueryLoading] = useState(false);
  const [queryError, setQueryError] = useState("");
  const [visibleNames, setVisibleNames] = useState<string[]>([]);
  const [columnsOpen, setColumnsOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState("");
  const exportController = useRef<AbortController | null>(null);

  async function request(path: string, signal: AbortSignal, body?: unknown): Promise<Response> {
    const response = await fetch(`${apiUrl}/data/${path}`, {
      method: body === undefined ? "GET" : "POST", signal,
      headers: { Authorization: `Bearer ${token}`, ...(body === undefined ? {} : { "Content-Type": "application/json" }) },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    if (signal.aborted) throw new DOMException("请求已取消", "AbortError");
    if (response.status === 401) { onExpired(); throw new Error("登录已失效，请重新登录"); }
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(typeof data.detail === "string" ? data.detail : "请求失败，请检查筛选条件后重试");
    }
    return response;
  }

  useEffect(() => {
    const controller = new AbortController();
    setCatalogLoading(true);
    setCatalogError("");
    setSelected(null);
    setTables([]);
    void (async () => {
      try {
        const response = await request("catalog", controller.signal);
        const catalog = await response.json() as { label: string; tables: Table[] };
        if (controller.signal.aborted) return;
        setTables(catalog.tables);
        setSourceLabel(catalog.label);
        setSelected(catalog.tables.find(table => table.schema_name === "olist" && table.name === "orders") ?? catalog.tables[0] ?? null);
      } catch (error) {
        if (!controller.signal.aborted) setCatalogError(error instanceof Error ? error.message : "读取数据表失败");
      } finally {
        if (!controller.signal.aborted) setCatalogLoading(false);
      }
    })();
    return () => controller.abort();
    // request 仅依赖此处列出的连接和身份，避免重渲染重复读取目录。
  }, [apiUrl, token, revision, onExpired]);

  useEffect(() => {
    const controller = new AbortController();
    exportController.current?.abort();
    exportController.current = null;
    setExporting(false);
    setExportError("");
    setDetails(null);
    setDetailsError("");
    setFilters([]);
    setMatch("all");
    setQuery(null);
    setResult(null);
    setQueryError("");
    setTab("preview");
    setColumnsOpen(false);
    setDetailsLoading(Boolean(selected));
    if (selected) {
      const parameters = new URLSearchParams({ schema_name: selected.schema_name, table_name: selected.name });
      void (async () => {
        try {
          const response = await request(`table?${parameters}`, controller.signal);
          const next = await response.json() as Details;
          if (controller.signal.aborted) return;
          setDetails(next);
          setVisibleNames(next.columns.map(column => column.name));
          setQuery({ schema_name: selected.schema_name, table_name: selected.name, filters: [], match: "all", page: 1, page_size: 20 });
        } catch (error) {
          if (!controller.signal.aborted) setDetailsError(error instanceof Error ? error.message : "读取字段失败");
        } finally {
          if (!controller.signal.aborted) setDetailsLoading(false);
        }
      })();
    }
    // 切表、身份变更时取消旧请求，迟到响应不能覆盖当前表。
    return () => { controller.abort(); exportController.current?.abort(); };
  }, [selected, apiUrl, token, onExpired]);

  useEffect(() => {
    const controller = new AbortController();
    setQueryLoading(Boolean(query));
    setQueryError("");
    setResult(null);
    if (query) void (async () => {
      try {
        const response = await request("query", controller.signal, query);
        const next = await response.json() as Result;
        if (!controller.signal.aborted) setResult(next);
      } catch (error) {
        if (!controller.signal.aborted) setQueryError(error instanceof Error ? error.message : "查询失败");
      } finally {
        if (!controller.signal.aborted) setQueryLoading(false);
      }
    })();
    return () => controller.abort();
  }, [query, apiUrl, token, onExpired]);

  const shownTables = useMemo(() => {
    const term = search.trim().toLowerCase();
    return tables.filter(table => `${table.label} ${table.comment ?? ""} ${table.name}`.toLowerCase().includes(term));
  }, [tables, search]);
  const shownColumns = details?.columns.filter(column => visibleNames.includes(column.name)) ?? [];
  const totalPages = result ? Math.max(1, Math.ceil(result.total / result.page_size)) : 1;
  const draftChanged = Boolean(query && (JSON.stringify(query.filters) !== JSON.stringify(filters.map(({ id: _id, ...filter }) => filter)) || query.match !== match));

  function changeFilter(id: string, update: Partial<Filter>) {
    setFilters(current => current.map(filter => filter.id === id ? { ...filter, ...update } : filter));
  }

  function runQuery(event?: FormEvent, reset = false) {
    event?.preventDefault();
    if (!selected || !details || queryLoading || exporting) return;
    setExportError("");
    if (reset) { setFilters([]); setMatch("all"); }
    setQuery({ schema_name: selected.schema_name, table_name: selected.name,
      filters: reset ? [] : filters.map(({ id: _id, ...filter }) => filter),
      match: reset ? "all" : match, page: 1, page_size: query?.page_size ?? 20 });
  }

  async function exportCsv() {
    if (!query || !result || exporting || !visibleNames.length) return;
    const controller = new AbortController();
    exportController.current = controller;
    setExporting(true);
    setExportError("");
    try {
      // 导出已执行的筛选结果和当前显示列，不使用尚未提交的筛选草稿。
      const response = await request("export", controller.signal, { ...query, columns: visibleNames });
      const blob = await response.blob();
      if (controller.signal.aborted) return;
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = `${selected?.label ?? "数据"}-查询结果.csv`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      if (!controller.signal.aborted) setExportError(error instanceof Error ? error.message : "导出失败");
    } finally {
      if (!controller.signal.aborted) { setExporting(false); exportController.current = null; }
    }
  }

  return <main className="data-explorer" hidden={hidden} aria-label="数据源与查询">
    <div className="data-explorer__body" style={tableListWidth === undefined ? undefined : { "--data-tables-width": `${tableListWidth}px` } as CSSProperties}>
      <aside className="data-tables" aria-label="数据表选择">
        <h2>当前数据源</h2>
        <div className="data-source"><DataIcon name="database" /><div><strong>{sourceLabel}</strong><small>{catalogLoading ? "正在连接…" : catalogError ? "连接不可用" : "已连接 · 只读"}</small></div></div>
        <label className="data-table-search"><span className="sr-only">搜索数据表</span><DataIcon name="search" /><input placeholder="搜索表名或业务内容" value={search} onChange={event => setSearch(event.target.value)} /></label>
        <div className="data-tables__heading"><h2>数据表</h2><button type="button" disabled={catalogLoading} onClick={() => setRevision(current => current + 1)} aria-label="刷新数据表" title="刷新数据表"><DataIcon name="refresh" /></button></div>
        {catalogLoading ? <p role="status" className="data-muted">正在读取数据表…</p> : catalogError ? <div role="alert" className="data-error"><p>{catalogError}</p><button type="button" onClick={() => setRevision(current => current + 1)}>重试连接</button></div>
          : tables.length === 0 ? <p className="data-muted">暂无可读取的数据表，请联系管理员检查只读权限。</p>
            : shownTables.length === 0 ? <p className="data-muted">没有匹配的数据表。</p>
              : <ul className="data-tables__list">{shownTables.map(table => <li key={`${table.schema_name}.${table.name}`}>
                <button type="button" className={selected === table ? "is-active" : ""} aria-pressed={selected === table} onClick={() => setSelected(table)}>
                  <DataIcon name={TABLE_ICONS[table.name] ?? "table"} /><strong>{table.label}</strong>
                </button>
              </li>)}</ul>}
        <ColumnResizeHandle label="调整数据表列表宽度" defaultWidth={window.innerWidth <= 1280 ? 175 : 190} minWidth={160} maxWidth={360} remainingWidth={360} onResize={setTableListWidth} />
      </aside>
      <section className="data-content" aria-label="数据展示与筛选">
        {selected ? <>
          <div className="data-content__title"><h2><DataIcon name={TABLE_ICONS[selected.name] ?? "table"} />{selected.label}</h2><p title={details?.comment || selected.comment || undefined}>{details?.comment || selected.comment || "暂无表说明，可在字段说明中查看结构。"}</p></div>
          {detailsLoading ? <p role="status" className="data-muted">正在读取字段说明…</p> : detailsError ? <div className="data-error" role="alert"><p>{detailsError}</p><button type="button" onClick={() => setSelected({ ...selected })}>重试</button></div> : details ? <>
            <div className="data-tabs" role="tablist" aria-label="数据视图">
              <button id="data-preview-tab" type="button" role="tab" aria-selected={tab === "preview"} aria-controls="data-preview" onClick={() => setTab("preview")}><DataIcon name="table" />数据预览</button>
              <button id="data-fields-tab" type="button" role="tab" aria-selected={tab === "fields"} aria-controls="data-fields" onClick={() => setTab("fields")}><DataIcon name="fields" />字段说明</button>
            </div>
            <section id="data-fields" role="tabpanel" aria-labelledby="data-fields-tab" hidden={tab !== "fields"} className="data-card data-fields">
              <div className="data-grid-scroll"><table><thead><tr><th>字段名称</th><th>数据类型</th><th>允许为空</th><th>说明</th></tr></thead><tbody>
                {details.columns.map(column => <tr key={column.name}><td><strong>{column.label}</strong>{column.primary_key ? <span className="data-key">主键</span> : null}</td><td>{column.data_type}</td><td>{column.nullable ? "是" : "否"}</td><td>{column.comment || "暂无说明"}</td></tr>)}
              </tbody></table></div>
            </section>
            <section id="data-preview" className="data-preview" role="tabpanel" aria-labelledby="data-preview-tab" hidden={tab !== "preview"}>
              <form className="data-card data-filters" onSubmit={event => runQuery(event)}>
                <div className="data-card__heading"><h3><DataIcon name="filter" />筛选条件</h3><select aria-label="筛选条件组合" value={match} onChange={event => setMatch(event.target.value as "all" | "any")}><option value="all">满足全部条件</option><option value="any">满足任一条件</option></select></div>
                <div className="data-filter-conditions">
                {filters.length === 0 ? <p className="data-muted">当前显示全部数据，添加条件可缩小查询范围。</p> : null}
                {filters.map((filter, index) => {
                  const column = details.columns.find(item => item.name === filter.column);
                  const noValue = filter.operator === "is_null" || filter.operator === "not_null";
                  const inputType = column?.kind === "date" ? "date" : column?.kind === "number" ? "number" : "text";
                  return <div className="data-filter-row" key={filter.id}>
                    <select aria-label={`条件 ${index + 1} 字段`} value={filter.column} onChange={event => {
                      const next = details.columns.find(item => item.name === event.target.value);
                      changeFilter(filter.id, { column: event.target.value, operator: next?.kind === "date" ? "between" : operators(next)[0], value: "", end_value: "" });
                    }}>{details.columns.map(item => <option key={item.name} value={item.name}>{item.label}</option>)}</select>
                    <select aria-label={`条件 ${index + 1} 比较方式`} value={filter.operator} onChange={event => changeFilter(filter.id, { operator: event.target.value as Operator })}>{operators(column).map(operator => <option key={operator} value={operator}>{OPERATORS[operator]}</option>)}</select>
                    <div className="data-filter-values">{!noValue ? <>
                      {column?.options.length || column?.kind === "boolean" ? <select aria-label={`条件 ${index + 1} 筛选值`} required value={filter.value} onChange={event => changeFilter(filter.id, { value: event.target.value })}>
                        <option value="">请选择</option>{(column.kind === "boolean" ? [{ value: "true", label: "是" }, { value: "false", label: "否" }] : column.options).map(option => <option key={option.value} value={option.value}>{option.label}</option>)}
                      </select> : <input aria-label={`条件 ${index + 1} 筛选值`} type={inputType} step="any" maxLength={2000} required value={filter.value} placeholder="输入筛选值" onChange={event => changeFilter(filter.id, { value: event.target.value })} />}
                      {filter.operator === "between" ? <><span>至</span><input aria-label={`条件 ${index + 1} 结束值`} type={inputType} step="any" maxLength={2000} required value={filter.end_value} onChange={event => changeFilter(filter.id, { end_value: event.target.value })} /></> : null}
                    </> : <span className="data-muted">无需填写筛选值</span>}</div>
                    <button type="button" className="data-filter-remove" aria-label={`删除条件 ${index + 1}`} onClick={() => setFilters(current => current.filter(item => item.id !== filter.id))}><DataIcon name="close" /></button>
                  </div>;
                })}
                </div>
                <div className="data-filter-actions"><button type="button" disabled={filters.length >= 12 || details.columns.length === 0} onClick={() => {
                  const column = details.columns.find(item => item.name === "order_status") ?? details.columns[0];
                  setFilters(current => [...current, { id: crypto.randomUUID(), column: column.name, operator: operators(column)[0], value: "", end_value: "" }]);
                }}><DataIcon name="plus" />添加条件</button><div><button type="button" disabled={queryLoading || exporting} onClick={() => runQuery(undefined, true)}><DataIcon name="reset" />重置</button><button type="submit" className="data-primary" disabled={queryLoading || exporting}><DataIcon name="search" />{queryLoading ? "查询中…" : "查询数据"}</button></div></div>
              </form>
              <section className="data-card data-results" aria-label="查询结果" aria-busy={queryLoading}>
                <div className="data-card__heading"><div className="data-result-count"><h3><DataIcon name="table" />查询结果</h3>{result ? <span>共 {result.total.toLocaleString()} 条</span> : null}</div><div className="data-result-tools">
                  <button type="button" aria-expanded={columnsOpen} onClick={() => setColumnsOpen(current => !current)}><DataIcon name="columns" />显示列</button>
                  <button type="button" disabled={!result || queryLoading || exporting || visibleNames.length === 0} onClick={() => void exportCsv()}><DataIcon name="download" />{exporting ? "导出中…" : "导出 CSV"}</button>
                </div></div>
                {columnsOpen ? <fieldset className="data-columns"><legend>选择显示字段</legend>{details.columns.map(column => <label key={column.name}><input type="checkbox" checked={visibleNames.includes(column.name)} disabled={visibleNames.length === 1 && visibleNames.includes(column.name)} onChange={event => setVisibleNames(current => event.target.checked ? [...current, column.name] : current.filter(name => name !== column.name))} />{column.label}</label>)}</fieldset> : null}
                {query?.filters.length ? <div className="data-applied"><span>{query.match === "all" ? "满足全部" : "满足任一"}</span>{query.filters.map((filter, index) => {
                  const column = details.columns.find(item => item.name === filter.column);
                  return <span className="data-applied__chip" key={index}>{column?.label} {OPERATORS[filter.operator]}{filter.operator === "is_null" || filter.operator === "not_null" ? "" : ` ${column ? displayValue(filter.value, column) : filter.value}${filter.operator === "between" ? ` 至 ${filter.end_value}` : ""}`}</span>;
                })}</div> : null}
                {draftChanged ? <p className="data-draft-notice">筛选条件已修改，点击“查询数据”后生效。</p> : null}
                {exportError ? <p className="data-error" role="alert">{exportError}</p> : null}
                {queryLoading ? <p className="data-result-message" role="status">正在查询数据…</p> : queryError ? <div className="data-result-message data-error" role="alert"><p>{queryError}</p><button type="button" onClick={() => query && setQuery({ ...query })}>重试查询</button></div> : result ? <>
                  <div className="data-grid-scroll"><table><thead><tr>{shownColumns.map(column => <th key={column.name}>{column.label}</th>)}</tr></thead><tbody>
                    {result.rows.map((row, index) => <tr key={index}>{shownColumns.map(column => <td key={column.name} title={displayValue(row[column.name], column)}><span className={column.name === "order_status" ? "data-status-value" : "data-cell-value"}>{displayValue(row[column.name], column)}</span></td>)}</tr>)}
                  </tbody></table>{result.rows.length === 0 ? <p className="data-result-message">{result.total === 0 ? "没有符合条件的数据，请调整筛选条件。" : "本页暂无数据，请返回前面的页面。"}</p> : null}</div>
                  <div className="data-pagination"><div><label>每页 <select aria-label="每页行数" disabled={exporting} value={query?.page_size ?? 20} onChange={event => query && setQuery({ ...query, page: 1, page_size: Number(event.target.value) as 20 | 50 | 100 })}><option value={20}>20 条</option><option value={50}>50 条</option><option value={100}>100 条</option></select></label><span>{result.total ? `第 ${(result.page - 1) * result.page_size + (result.rows.length ? 1 : 0)}–${(result.page - 1) * result.page_size + result.rows.length} 条` : "共 0 条"}</span></div>
                    <div><button type="button" disabled={result.page <= 1 || exporting} onClick={() => query && setQuery({ ...query, page: result.page - 1 })}><DataIcon name="left" />上一页</button><span>{result.page} / {totalPages}</span><button type="button" disabled={result.page >= totalPages || result.page >= 50_000 || exporting} onClick={() => query && setQuery({ ...query, page: result.page + 1 })}>下一页<DataIcon name="right" /></button></div>
                  </div>
                </> : null}
              </section>
            </section>
          </> : null}
        </> : <div className="data-content__empty"><DataIcon name="database" /><h2>选择一张数据表</h2><p>在左侧查看可用数据表，预览数据并设置筛选条件。</p></div>}
      </section>
    </div>
  </main>;
}
