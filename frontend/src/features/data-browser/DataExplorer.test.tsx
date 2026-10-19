import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi, type Mock } from "vitest";
import DataExplorer from "./DataExplorer";

const columns = [
  { name: "order_id", label: "订单编号", kind: "text", data_type: "text", nullable: false, primary_key: true, comment: "匿名订单编号", options: [] },
  { name: "order_status", label: "订单状态", kind: "text", data_type: "text", nullable: true, primary_key: false, comment: null, options: [{ value: "delivered", label: "已送达" }] },
  { name: "order_purchase_timestamp", label: "下单时间", kind: "date", data_type: "timestamp", nullable: true, primary_key: false, comment: null, options: [] },
];
const tables = [
  { schema_name: "olist", name: "orders", label: "订单", comment: "每行一笔订单" },
  { schema_name: "olist", name: "customers", label: "客户", comment: "客户资料" },
];
const props = { apiUrl: "http://api", token: "valid", hidden: false, onExpired: vi.fn() };
const response = (body: unknown, status = 200) => ({ ok: status === 200, status, json: async () => body });
let fetcher: Mock<(url: string, init?: RequestInit) => Promise<unknown>>;

beforeEach(() => {
  vi.clearAllMocks();
  fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (url.endsWith("/catalog")) return response({ label: "Olist 电商数据库", tables });
    if (url.includes("/table?")) return response({ label: "订单", comment: "每行一笔订单", columns });
    const query = JSON.parse(init?.body as string);
    return response({ columns, rows: [{ order_id: "one", order_status: "delivered", order_purchase_timestamp: "2018-03-31T15:00:00" }], total: 41, page: query.page, page_size: query.page_size });
  });
  vi.stubGlobal("fetch", fetcher);
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

it("显示中文表名、字段与实际数据，不包含英文表标识和会话分析按钮", async () => {
  render(<DataExplorer {...props} />);
  expect(await screen.findByText("one")).toBeTruthy();
  expect(screen.getByText("已送达")).toBeTruthy();
  expect(screen.queryByText("orders")).toBeNull();
  expect(screen.queryByText("olist.orders")).toBeNull();
  expect(screen.queryByText(/在会话中分析/)).toBeNull();
  fireEvent.click(screen.getByRole("tab", { name: "字段说明" }));
  expect(within(screen.getByRole("tabpanel")).getByText("匿名订单编号")).toBeTruthy();
});

it("数据表列表可调整宽度，切换数据表保留宽度，双击恢复默认", async () => {
  render(<DataExplorer {...props} />);
  await screen.findByText("one");
  const handle = screen.getByRole("separator", { name: "调整数据表列表宽度" });
  fireEvent.keyDown(handle, { key: "ArrowRight" });
  const body = handle.closest(".data-explorer__body") as HTMLElement;
  expect(body.style.getPropertyValue("--data-tables-width")).toBe("191px");
  fireEvent.click(screen.getByRole("button", { name: "客户" }));
  await screen.findByText("one");
  expect(body.style.getPropertyValue("--data-tables-width")).toBe("191px");
  fireEvent.doubleClick(handle);
  expect(body.style.getPropertyValue("--data-tables-width")).toBe("");
});

it("筛选草稿在查询后生效，翻页沿用已提交条件", async () => {
  render(<DataExplorer {...props} />);
  await screen.findByText("one");
  fireEvent.click(screen.getByRole("button", { name: "添加条件" }));
  fireEvent.change(screen.getByLabelText("条件 1 筛选值"), { target: { value: "delivered" } });
  expect(screen.getByText(/筛选条件已修改/)).toBeTruthy();
  expect(fetcher).toHaveBeenCalledTimes(3);
  fireEvent.click(screen.getByRole("button", { name: "查询数据" }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(4));
  await screen.findByText("one");
  const applied = JSON.parse(fetcher.mock.calls[3][1]!.body as string);
  expect(applied.filters).toEqual([{ column: "order_status", operator: "eq", value: "delivered", end_value: "" }]);
  fireEvent.change(screen.getByLabelText("条件 1 字段"), { target: { value: "order_purchase_timestamp" } });
  expect((screen.getByLabelText("条件 1 比较方式") as HTMLSelectElement).value).toBe("between");
  fireEvent.click(screen.getByRole("button", { name: "下一页" }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(5));
  const paginated = JSON.parse(fetcher.mock.calls[4][1]!.body as string);
  expect(paginated.page).toBe(2);
  expect(paginated.filters).toEqual(applied.filters);
});

it("切换表取消旧查询，迟到响应不覆盖当前表", async () => {
  let resolve!: (value: unknown) => void;
  const original = fetcher.getMockImplementation()!;
  fetcher.mockImplementation((url: string, init?: RequestInit) => url.endsWith("/query") && JSON.parse(init?.body as string).table_name === "orders"
    ? new Promise(done => { resolve = done; }) : original(url, init));
  render(<DataExplorer {...props} />);
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(3));
  const oldSignal = fetcher.mock.calls[2][1]!.signal as AbortSignal;
  fireEvent.click(screen.getByRole("button", { name: "客户" }));
  await screen.findByText("one");
  expect(oldSignal.aborted).toBe(true);
  await act(async () => resolve(response({ columns, rows: [{ order_id: "stale" }], total: 1, page: 1, page_size: 20 })));
  expect(screen.queryByText("stale")).toBeNull();
});

it("导出已提交条件和显示列，后端导出上限错误可见", async () => {
  const original = fetcher.getMockImplementation()!;
  fetcher.mockImplementation((url: string, init?: RequestInit) => url.endsWith("/export") ? Promise.resolve(response({ detail: "查询结果过多，请缩小范围" }, 422)) : original(url, init));
  render(<DataExplorer {...props} />);
  await screen.findByText("one");
  fireEvent.click(screen.getByRole("button", { name: "显示列" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "订单状态" }));
  fireEvent.click(screen.getByRole("button", { name: "添加条件" }));
  fireEvent.change(screen.getByLabelText("条件 1 筛选值"), { target: { value: "delivered" } });
  fireEvent.click(screen.getByRole("button", { name: "导出 CSV" }));
  expect(await screen.findByText("查询结果过多，请缩小范围")).toBeTruthy();
  const payload = JSON.parse(fetcher.mock.calls[3][1]!.body as string);
  expect(payload.filters).toEqual([]);
  expect(payload.columns).toEqual(["order_id", "order_purchase_timestamp"]);
});

it("空结果提供调整筛选提示，重置重新查询全部数据", async () => {
  const original = fetcher.getMockImplementation()!;
  fetcher.mockImplementation(async (url: string, init?: RequestInit) => url.endsWith("/query")
    ? response({ columns, rows: [], total: 0, page: 1, page_size: 20 }) : original(url, init));
  render(<DataExplorer {...props} />);
  expect(await screen.findByText("没有符合条件的数据，请调整筛选条件。")).toBeTruthy();
  expect((screen.getByRole("button", { name: "下一页" }) as HTMLButtonElement).disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "添加条件" }));
  fireEvent.click(screen.getByRole("button", { name: "重置" }));
  await waitFor(() => expect(fetcher).toHaveBeenCalledTimes(4));
  expect(screen.queryByLabelText("条件 1 字段")).toBeNull();
  expect(JSON.parse(fetcher.mock.calls[3][1]!.body as string).filters).toEqual([]);
});

it("连接失败可重试，身份失效由应用处理", async () => {
  fetcher.mockResolvedValueOnce(response({ detail: "数据库未启用" }, 503));
  render(<DataExplorer {...props} />);
  expect(await screen.findByText("数据库未启用")).toBeTruthy();
  fetcher.mockResolvedValueOnce(response({}, 401));
  fireEvent.click(screen.getByRole("button", { name: "重试连接" }));
  await waitFor(() => expect(props.onExpired).toHaveBeenCalledOnce());
});
