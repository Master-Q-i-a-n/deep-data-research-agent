import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App, { messageMarkdown, normalizeContextUsageEvent, normalizeWebSearchEvent } from "./App";

const submit = vi.fn();
const stop = vi.fn();
const switchThread = vi.fn();
const joinStream = vi.fn();
const listRuns = vi.fn();
const createRun = vi.fn();
const getRun = vi.fn();
const cancelRun = vi.fn();
const cancelQueuedRun = vi.fn();
const clearQueue = vi.fn();
let capturedOptions: Record<string, unknown> | null = null;
let capturedClientConfig: Record<string, unknown> | null = null;

type TestMessage = {
  id: string;
  type: string;
  content: unknown;
  name?: string;
  status?: string;
  tool_calls?: Array<{ id: string; name: string; args: Record<string, unknown> }>;
  tool_call_id?: string;
  artifact?: unknown;
};

function createStreamState() {
  return {
    values: {
      context_usage: undefined as {
        used_tokens: number;
        max_input_tokens: number;
        provider_version: number;
      } | undefined,
      todos: [
        { content: "拆分网页采集任务", status: "completed" as const },
        { content: "等待 crawl-worker 返回", status: "in_progress" as const },
      ],
      async_tasks: {
        "task-123456789": {
          task_id: "task-123456789",
          thread_id: "task-123456789",
          run_id: "run-123456789",
          agent_name: "crawl-worker",
          status: "running" as const,
          last_checked_at: "2026-07-28T11:42:10Z",
        },
      },
    },
    messages: [
      { id: "human-1", type: "human", content: "分析 Tavily 文档" },
      {
        id: "ai-1",
        type: "ai",
        content: "",
        tool_calls: [
          { id: "call-1", name: "start_async_task", args: { subagent_type: "crawl-worker" } },
        ],
      },
      { id: "tool-1", type: "tool", tool_call_id: "call-1", content: "task_id: task-123456789" },
    ] as TestMessage[],
    isLoading: false,
    error: null,
    subagents: new Map<string, Record<string, unknown>>(),
    interrupt: undefined as {
      id?: string;
      value?: {
        action_requests: Array<{ name: string; args: Record<string, unknown> }>;
        review_configs: Array<{
          action_name: string;
          allowed_decisions: Array<"approve" | "reject" | "respond">;
        }>;
      };
    } | undefined,
    submit,
    stop,
    switchThread,
    joinStream,
    client: {
      runs: {
        list: listRuns,
        create: createRun,
        get: getRun,
        cancel: cancelRun,
      },
    },
    queue: {
      entries: [] as Array<{
        id: string;
        values: { messages: Array<{ type: string; content: string }> };
        createdAt: Date;
      }>,
      size: 0,
      cancel: cancelQueuedRun,
      clear: clearQueue,
    },
  };
}

let streamState = createStreamState();

function enableDetailedMode() {
  fireEvent.click(screen.getByRole("button", { name: "打开账户菜单" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "设置" }));
  fireEvent.click(screen.getByRole("switch", { name: "详细模式" }));
  fireEvent.click(screen.getByRole("button", { name: "关闭设置" }));
}

function expectDetailedMode(enabled: boolean) {
  fireEvent.click(screen.getByRole("button", { name: "打开账户菜单" }));
  fireEvent.click(screen.getByRole("menuitem", { name: "设置" }));
  expect(screen.getByRole("switch", { name: "详细模式" }).getAttribute("aria-checked")).toBe(String(enabled));
  fireEvent.click(screen.getByRole("button", { name: "关闭设置" }));
}

type FetchHandler = (
  input: string | URL | Request,
  init?: RequestInit,
) => Promise<unknown>;

function withAuthenticatedUser(handler: FetchHandler) {
  return vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith("/auth/me")) {
      return {
        ok: true,
        json: async () => ({
          user: { id: "user-a", username: "Alice" },
        }),
      };
    }
    if (url.endsWith("/memories/settings") && !init?.method) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ failure_lesson_saving_enabled: true }),
      };
    }
    if (url.endsWith("/model-provider") && !init?.method) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          configured: true,
          provider: {
            provider_name: "dashscope",
            provider_type: "chat_completions",
            base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
            model_name: "qwen-plus",
            has_api_key: true,
            api_key_hint: "1234",
            version: 1,
            updated_at: "2026-08-28T10:00:00",
          },
        }),
      };
    }
    return handler(input, init);
  });
}

vi.mock("@langchain/react", () => ({
  useStream: (options: Record<string, unknown>) => {
    capturedOptions = options;
    return streamState;
  },
}));

vi.mock("@langchain/langgraph-sdk/client", () => ({
  Client: class MockClient {
    constructor(config: Record<string, unknown>) {
      capturedClientConfig = config;
    }

    get runs() {
      return streamState.client.runs;
    }
  },
}));

beforeEach(() => {
  streamState = createStreamState();
  submit.mockResolvedValue(undefined);
  listRuns.mockResolvedValue([]);
  createRun.mockResolvedValue({
    run_id: "queued-run",
    thread_id: "thread-current",
    assistant_id: "supervisor",
    status: "pending",
    created_at: "2026-08-22T09:00:00Z",
    updated_at: "2026-08-22T09:00:00Z",
    metadata: {},
    multitask_strategy: "enqueue",
  });
  getRun.mockResolvedValue({
    run_id: "run-active",
    thread_id: "thread-current",
    assistant_id: "supervisor",
    status: "running",
    created_at: "2026-08-22T09:00:00Z",
    updated_at: "2026-08-22T09:00:00Z",
    metadata: {},
    multitask_strategy: "enqueue",
  });
  cancelRun.mockResolvedValue(undefined);
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: string | URL | Request) => {
    const url = String(input);
    if (url.endsWith("/auth/me")) {
      return { ok: true, json: async () => ({ user: { id: "user-a", username: "Alice" } }) };
    }
    if (url.endsWith("/memories/settings")) {
      return { ok: true, status: 200, json: async () => ({ failure_lesson_saving_enabled: true }) };
    }
    if (url.endsWith("/model-provider")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          configured: true,
          provider: {
            provider_name: "dashscope",
            provider_type: "chat_completions",
            base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
            model_name: "qwen-plus",
            has_api_key: true,
            api_key_hint: "1234",
            version: 1,
            updated_at: "2026-08-28T10:00:00",
          },
        }),
      };
    }
    if (url.endsWith("/async-tasks/task-123456789/cancel")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          task: {
            task_id: "task-123456789",
            thread_id: "task-123456789",
            run_id: "run-123456789",
            agent_name: "crawl-worker",
            status: "cancelled",
          },
          cancelled_runs: 1,
        }),
      };
    }
    return {
      ok: true,
      json: async () => (url.endsWith("/async-tasks/status") ? { tasks: [] } : []),
    };
  }));
});

afterEach(() => {
  cleanup();
  submit.mockReset();
  stop.mockReset();
  switchThread.mockReset();
  joinStream.mockReset();
  listRuns.mockReset();
  createRun.mockReset();
  getRun.mockReset();
  cancelRun.mockReset();
  cancelQueuedRun.mockReset();
  clearQueue.mockReset();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  capturedOptions = null;
  capturedClientConfig = null;
  window.history.replaceState({}, "", "http://localhost:5174/");
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe("研究工作台", () => {
  it("将安全的 URL citation 渲染为行内 Markdown 链接", () => {
    const markdown = messageMarkdown([{
      type: "text",
      text: "结果【1】",
      annotations: [{
        type: "url_citation",
        start_index: 2,
        end_index: 5,
        title: "OpenAI",
        url: "https://developers.openai.com/api/docs/guides/tools-web-search",
      }],
    }]);

    expect(markdown).toContain("[【1】](<https://developers.openai.com/api/docs/guides/tools-web-search>)");
    expect(messageMarkdown([{
      type: "text",
      text: "不安全引用",
      annotations: [{ type: "url_citation", start_index: 0, end_index: 5, url: "javascript:alert(1)" }],
    }])).toBe("不安全引用");
  });

  it("校验网页搜索自定义事件并过滤重复或危险来源", () => {
    expect(normalizeWebSearchEvent({
      type: "web_search_progress",
      phase: "completed",
      item_id: "ws-1",
      output_index: 0,
      sequence_number: 3,
      action: { type: "search", query: "Responses API" },
      sources: [
        { title: "OpenAI", url: "https://developers.openai.com/" },
        { title: "重复", url: "https://developers.openai.com/" },
        { title: "危险", url: "javascript:alert(1)" },
      ],
    })?.sources).toEqual([
      { title: "OpenAI", url: "https://developers.openai.com/" },
    ]);
  });

  it("校验上下文事件，并按 Provider 版本显示圆形占比", async () => {
    expect(normalizeContextUsageEvent({
      type: "context_usage",
      phase: "before_model",
      used_tokens: 700,
      max_input_tokens: 1_000,
      provider_version: 1,
    })).toMatchObject({ used_tokens: 700, max_input_tokens: 1_000 });
    expect(normalizeContextUsageEvent({
      type: "context_usage",
      phase: "before_model",
      used_tokens: 1,
      max_input_tokens: 0,
      provider_version: 1,
    })).toBeNull();

    streamState.values.context_usage = {
      used_tokens: 700,
      max_input_tokens: 1_000,
      provider_version: 1,
    };
    render(<App />);
    await screen.findByText("Alice");
    fireEvent.click(screen.getByRole("button", { name: "打开账户菜单" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "设置" }));
    await screen.findByDisplayValue("qwen-plus");
    fireEvent.click(screen.getByRole("button", { name: "关闭设置" }));
    expect(screen.getByRole("status", {
      name: "上下文窗口：70% 已用，已用 700 Token，共 1k",
    }).className).toContain("context-window--warning");
    const composerIndicator = screen.getByRole("status", {
      name: "上下文窗口：70% 已用，已用 700 Token，共 1k",
    });
    expect(composerIndicator.closest(".composer__field")).not.toBeNull();
    expect(composerIndicator.closest(".topbar")).toBeNull();
    const resizeDivider = screen.getByRole("separator", { name: "调整输入框高度" });
    expect(resizeDivider.getAttribute("aria-valuenow")).toBe("54");
    fireEvent.keyDown(resizeDivider, { key: "ArrowUp" });
    expect(resizeDivider.getAttribute("aria-valuenow")).toBe("78");
    expect(document.querySelector<HTMLElement>(".main-panel")?.style.getPropertyValue(
      "--composer-input-height",
    )).toBe("78px");
    const onCustomEvent = capturedOptions?.onCustomEvent as (event: unknown) => void;
    await act(async () => onCustomEvent({
      type: "context_usage",
      phase: "after_model",
      used_tokens: 850,
      max_input_tokens: 1_000,
      provider_version: 1,
    }));

    const indicator = screen.getByRole("status", {
      name: "上下文窗口：85% 已用，已用 850 Token，共 1k",
    });
    expect(indicator.className).toContain("context-window--danger");

    const onFinish = capturedOptions?.onFinish as (state: unknown, run: unknown) => void;
    await act(async () => onFinish({
      context_usage: {
        used_tokens: 860,
        max_input_tokens: 1_000,
        provider_version: 1,
      },
    }, undefined));
    expect(screen.getByRole("status", {
      name: "上下文窗口：86% 已用，已用 860 Token，共 1k",
    })).toBeTruthy();

    const onError = capturedOptions?.onError as (error: unknown, run: unknown) => void;
    await act(async () => onError(new Error("stream ended"), undefined));
    expect(screen.getByRole("status", {
      name: "上下文窗口：86% 已用，已用 860 Token，共 1k",
    })).toBeTruthy();

    await act(async () => onCustomEvent({
      type: "context_usage",
      phase: "after_model",
      used_tokens: 900,
      max_input_tokens: 1_000,
      provider_version: 99,
    }));
    expect(screen.queryByLabelText(/90% 已用/)).toBeNull();
  });

  it("实时显示搜索阶段、查询和来源，且忽略乱序事件", async () => {
    streamState.isLoading = true;
    streamState.values.todos = [];
    streamState.messages = [{ id: "human-search", type: "human", content: "查找最新资料" }];
    const view = render(<App />);
    await screen.findByText("Alice");

    const onCustomEvent = capturedOptions?.onCustomEvent as (event: unknown) => void;
    await act(async () => onCustomEvent({
      type: "web_search_progress",
      phase: "searching",
      item_id: "ws-live",
      output_index: 0,
      sequence_number: 2,
      action: { type: "search", query: "Responses API 最新文档" },
    }));
    expect(screen.getByText("正在搜索网页：Responses API 最新文档")).toBeTruthy();

    await act(async () => onCustomEvent({
      type: "web_search_progress",
      phase: "searching",
      item_id: "ws-live",
      output_index: 0,
      sequence_number: 1,
      action: { type: "search", query: "过期查询" },
    }));
    expect(screen.queryByText(/过期查询/)).toBeNull();

    await act(async () => onCustomEvent({
      type: "web_search_progress",
      phase: "completed",
      item_id: "ws-live",
      output_index: 0,
      sequence_number: 3,
      action: { type: "search", query: "Responses API 最新文档" },
      sources: [{ title: "OpenAI Docs", url: "https://developers.openai.com/" }],
    }));
    expect(screen.getByText("网页搜索完成，正在整理结果…")).toBeTruthy();

    enableDetailedMode();
    expect(screen.getByText("正在整理")).toBeTruthy();
    const source = screen.getByRole("link", { name: "developers.openai.com" });
    expect(source.getAttribute("href")).toBe("https://developers.openai.com/");
    expect(source.getAttribute("target")).toBe("_blank");

    // 单次搜索完成后仍可能在生成答案；整轮运行结束时状态必须随之收敛。
    streamState.isLoading = false;
    view.rerender(<App />);
    expect(screen.getByText("已完成")).toBeTruthy();
    expect(screen.queryByText("正在整理")).toBeNull();
  });

  it("从持久化 Responses 内容块恢复搜索卡和可点击行内引用", () => {
    streamState.values.todos = [];
    streamState.messages = [
      { id: "human-history", type: "human", content: "查询资料" },
      {
        id: "ai-history",
        type: "ai",
        content: [
          {
            type: "web_search_call",
            id: "ws-history",
            index: 0,
            status: "completed",
            action: {
              type: "search",
              query: "LangChain Responses",
              sources: [{ type: "url", url: "https://python.langchain.com/docs/" }],
            },
          },
          {
            type: "text",
            text: "参考【1】",
            annotations: [{
              type: "url_citation",
              start_index: 2,
              end_index: 5,
              title: "LangChain",
              url: "https://python.langchain.com/docs/",
            }],
          },
        ],
      },
    ];
    render(<App />);
    enableDetailedMode();

    expect(screen.getByText("LangChain Responses")).toBeTruthy();
    const searchRow = screen.getByText("已搜索网页").closest("section");
    expect(searchRow).not.toBeNull();
    expect(within(searchRow as HTMLElement).getByText("已完成")).toBeTruthy();
    expect(within(searchRow as HTMLElement).queryByText("正在整理")).toBeNull();
    expect(screen.getByRole("link", { name: "python.langchain.com" })).toBeTruthy();
    expect(screen.getByRole("link", { name: "【1】" }).getAttribute("href"))
      .toBe("https://python.langchain.com/docs/");
  });
  it("将相对 API 地址解析为 LangGraph SDK 可使用的绝对地址", async () => {
    vi.stubEnv("VITE_LANGGRAPH_API_URL", "/api");

    render(<App />);
    await screen.findByText("Alice");

    expect(capturedClientConfig?.apiUrl).toBe(`${window.location.origin}/api`);
  });

  it("新任务取得 thread ID 后立即刷新左侧会话记录", async () => {
    let searchCount = 0;
    vi.stubGlobal("fetch", withAuthenticatedUser(vi.fn().mockImplementation(async (input: string | URL | Request) => {
      if (String(input).endsWith("/threads/search")) {
        searchCount += 1;
        return {
          ok: true,
          json: async () => searchCount === 1 ? [] : [{
            thread_id: "thread-new",
            status: "busy",
            metadata: { title: "刚创建的研究任务" },
          }],
        };
      }
      return { ok: true, json: async () => ({}) };
    })));

    render(<App />);
    await waitFor(() => expect(searchCount).toBe(1));

    await act(async () => {
      (capturedOptions?.onThreadId as (id: string) => void)("thread-new");
    });

    await waitFor(() => expect(screen.getByText("刚创建的研究任务")).toBeTruthy());
  });

  it("刷新后根据 busy thread 兜底重新加入服务端活动 run", async () => {
    window.history.replaceState({}, "", "http://localhost:5174/?thread=thread-busy");
    const activeRun = {
      run_id: "run-active",
      thread_id: "thread-busy",
      assistant_id: "supervisor",
      status: "running",
      created_at: "2026-08-13T15:24:24Z",
      updated_at: "2026-08-13T15:25:24Z",
      metadata: {},
      multitask_strategy: null,
    };
    listRuns.mockImplementation(async (_threadId, options) => (
      options?.status === "running" ? [activeRun] : []
    ));
    joinStream.mockResolvedValue(undefined);
    vi.stubGlobal("fetch", withAuthenticatedUser(vi.fn().mockImplementation(async (input: string | URL | Request) => {
      if (String(input).endsWith("/threads/search")) {
        return {
          ok: true,
          json: async () => [{
            thread_id: "thread-busy",
            status: "busy",
            metadata: { title: "执行中的任务" },
          }],
        };
      }
      return { ok: true, json: async () => ({}) };
    })));

    render(<App />);

    await waitFor(() => expect(listRuns).toHaveBeenCalledWith(
      "thread-busy",
      expect.objectContaining({ limit: 100, status: "running" }),
    ));
    expect(listRuns).toHaveBeenCalledWith(
      "thread-busy",
      expect.objectContaining({ limit: 100, status: "pending" }),
    );
    await waitFor(() => expect(joinStream).toHaveBeenCalledWith("run-active"));
  });

  it("恢复登录账户并向 LangGraph SDK 发送认证头", async () => {
    window.localStorage.setItem("deep-data-auth-token", "token-a");
    render(<App />);

    expect(await screen.findByText("Alice")).toBeTruthy();
    expectDetailedMode(false);
    expect(screen.queryByLabelText("研究执行状态")).toBeNull();
    expect(capturedOptions?.defaultHeaders).toEqual({ Authorization: "Bearer token-a" });
    expect(capturedOptions?.filterSubagentMessages).toBe(true);
    expect(capturedOptions?.onFinish).toEqual(expect.any(Function));
    expect((capturedOptions?.onFinish as ((state: unknown, run: unknown) => void)).length).toBe(2);
  });

  it("未登录时保留首页并锁定任务功能", async () => {
    streamState.messages = [];
    streamState.values.async_tasks = {} as typeof streamState.values.async_tasks;
    const fetchMock = vi.fn(async (input: string | URL | Request) => {
      if (String(input).endsWith("/auth/me")) {
        return {
          ok: false,
          status: 401,
          json: async () => ({ detail: "请先登录" }),
        };
      }
      if (String(input).endsWith("/memories/settings")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ failure_lesson_saving_enabled: true }),
        };
      }
      return { ok: true, status: 200, json: async () => [] };
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByText("需要认证")).toBeTruthy();
    expect(screen.getByText("请登录或注册后开始研究任务。")).toBeTruthy();
    expect((screen.getByRole("button", { name: "开始新任务" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByLabelText("选择本地表格文件") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("登录后可开始研究任务") as HTMLTextAreaElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "发送分析任务" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "登录" }) as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByRole("button", { name: "注册" }) as HTMLButtonElement).disabled).toBe(false);

    enableDetailedMode();
    expectDetailedMode(true);
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/threads/search"))).toBe(false);
    expect(fetchMock.mock.calls.some(([input]) => String(input).endsWith("/async-tasks/status"))).toBe(false);
  });

  it("受保护请求返回 401 后清除失效令牌并重新锁定", async () => {
    window.localStorage.setItem("deep-data-auth-token", "expired-token");
    const fetchMock = vi.fn(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      const headers = init?.headers as Record<string, string> | undefined;
      if (url.endsWith("/auth/me") && headers?.Authorization) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ user: { id: "user-a", username: "Alice" } }),
        };
      }
      if (url.endsWith("/auth/me") || url.endsWith("/threads/search")) {
        return { ok: false, status: 401, json: async () => ({ detail: "登录已失效，请重新登录" }) };
      }
      return { ok: true, status: 200, json: async () => [] };
    });
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);

    expect(await screen.findByText("需要认证")).toBeTruthy();
    expect(window.localStorage.getItem("deep-data-auth-token")).toBeNull();
    expect(screen.getByText("登录已失效，请重新登录")).toBeTruthy();
    expect((screen.getByRole("button", { name: "开始新任务" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("认证表单直接展示服务端限流提示", async () => {
    vi.stubGlobal("fetch", vi.fn(async (input: string | URL | Request) => {
      if (String(input).endsWith("/auth/login")) {
        return {
          ok: false,
          status: 429,
          json: async () => ({ detail: "登录尝试过于频繁，请稍后再试" }),
        };
      }
      return { ok: false, status: 401, json: async () => ({ detail: "请先登录" }) };
    }));
    render(<App />);
    await screen.findByText("需要认证");

    fireEvent.click(screen.getByRole("button", { name: "登录" }));
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "Alice" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "password8" } });
    fireEvent.click(screen.getByRole("button", { name: "登录并进入个人空间" }));

    expect(await screen.findByText("登录尝试过于频繁，请稍后再试")).toBeTruthy();
  });

  it("简洁模式显示全部可见轮次且每轮只保留最终 AI 回复", async () => {
    window.history.replaceState({}, "", "http://localhost:5174/?thread=thread-current");
    listRuns.mockImplementation(async (_threadId, options) => options?.status === "running" ? [{
      run_id: "run-active",
      thread_id: "thread-current",
      assistant_id: "supervisor",
      status: "running",
      created_at: "2026-08-22T09:00:00Z",
      updated_at: "2026-08-22T09:00:00Z",
      metadata: {},
      multitask_strategy: null,
    }] : []);
    streamState.values.async_tasks = {} as typeof streamState.values.async_tasks;
    streamState.isLoading = true;
    streamState.messages = [
      { id: "human-old", type: "human", content: "旧问题" },
      { id: "ai-old", type: "ai", content: "旧回答" },
      { id: "human-current", type: "human", content: "当前问题" },
      { id: "ai-current-1", type: "ai", content: "先检查数据。" },
      {
        id: "ai-tool",
        type: "ai",
        content: "",
        tool_calls: [{ id: "call-skill", name: "assign_skill", args: { skill_name: "demo" } }],
      },
      { id: "tool-skill", type: "tool", tool_call_id: "call-skill", content: "assigned" },
      { id: "monitor", name: "async-task-monitor", type: "human", content: "内部续跑消息" },
      { id: "ai-current-2", type: "ai", content: "最终回答。" },
    ];

    const view = render(<App />);

    expect(screen.getByText("旧问题")).toBeTruthy();
    expect(screen.getByText("旧回答")).toBeTruthy();
    expect(screen.getByText("当前问题")).toBeTruthy();
    expect(screen.getByText("最终回答。")).toBeTruthy();
    expect(screen.queryByText("先检查数据。")).toBeNull();
    expect(screen.queryByText("内部续跑消息")).toBeNull();
    expect(screen.queryByText("分配 Skill")).toBeNull();
    expect(view.container.querySelectorAll(".compact-turn__output")).toHaveLength(2);

    streamState.messages = [
      ...streamState.messages.slice(0, -1),
      { id: "ai-current-2", type: "ai", content: "最终回答继续流式增长。" },
    ];
    view.rerender(<App />);
    expect(view.container.querySelectorAll(".compact-turn__output")).toHaveLength(2);
    expect(screen.getByText(/最终回答继续流式增长。/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "停止回答" }));
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledWith(
      "http://127.0.0.1:2024/threads/thread-current/cancel-all",
      expect.objectContaining({ method: "POST" }),
    ));
    expect(cancelRun).not.toHaveBeenCalled();
    expect(stop).not.toHaveBeenCalled();

    enableDetailedMode();
    expectDetailedMode(true);
    expect(screen.getByText("旧问题")).toBeTruthy();
    expect(screen.getByText("分配 Skill")).toBeTruthy();
    expect(screen.getByLabelText("研究执行状态")).toBeTruthy();

    enableDetailedMode();
    expect(screen.getByText("旧问题")).toBeTruthy();
    expect(screen.queryByText("先检查数据。")).toBeNull();
    expect(screen.queryByLabelText("研究执行状态")).toBeNull();
  });

  it("当前轮在同一个 AI 卡中显示流式文本与执行阶段", () => {
    streamState.isLoading = true;
    streamState.values.todos = [];
    streamState.messages = [
      { id: "human-live", type: "human", content: "分析数据库" },
      {
        id: "ai-task",
        type: "ai",
        content: "",
        tool_calls: [{
          id: "call-task",
          name: "task",
          args: { subagent_type: "data-analyst" },
        }],
      },
    ];
    const view = render(<App />);

    expect(screen.getByText("正在调用 data-analyst…")).toBeTruthy();
    expect(view.container.querySelectorAll(".compact-turn__output")).toHaveLength(1);

    streamState.messages = [
      ...streamState.messages,
      { id: "tool-task", type: "tool", tool_call_id: "call-task", content: "done" },
    ];
    view.rerender(<App />);
    expect(screen.getByText("正在整理工具结果…")).toBeTruthy();

    streamState.messages = [{ id: "human-live", type: "human", content: "分析数据库" }];
    streamState.values.todos = [{ content: "核验数据库结构", status: "in_progress" }];
    view.rerender(<App />);
    expect(screen.getByText("正在核验数据库结构…")).toBeTruthy();

    streamState.values.todos = [];
    view.rerender(<App />);
    expect(screen.getByText("正在规划任务…")).toBeTruthy();

    streamState.messages = [
      { id: "human-live", type: "human", content: "分析数据库" },
      { id: "ai-live", type: "ai", content: "正在形成最终结论" },
    ];
    view.rerender(<App />);
    expect(screen.getByText("正在形成最终结论")).toBeTruthy();
    expect(screen.getByText("生成中")).toBeTruthy();
    expect(view.container.querySelectorAll(".compact-turn__output")).toHaveLength(1);
  });

  it("排队消息只进入等待卡且不会替代当前执行轮次", async () => {
    window.history.replaceState({}, "", "http://localhost:5174/?thread=thread-current");
    streamState.isLoading = true;
    streamState.values.todos = [];
    streamState.messages = [
      { id: "human-active", type: "human", content: "先分析当前数据" },
      { id: "ai-active", type: "ai", content: "正在计算核心指标" },
    ];

    const view = render(<App />);
    await screen.findByText("Alice");
    fireEvent.change(screen.getByLabelText("补充要求或纠正方向"), {
      target: { value: "再增加区域对比" },
    });
    fireEvent.click(screen.getByRole("button", { name: "排队发送消息" }));

    expect(screen.getByText("正在计算核心指标")).toBeTruthy();
    expect(await screen.findByText("再增加区域对比")).toBeTruthy();
    expect(view.container.querySelectorAll(".compact-turn")).toHaveLength(1);
  });

  it("已结束但没有 AI 回复的轮次显示明确状态", () => {
    streamState.values.todos = [];
    streamState.messages = [{ id: "human-empty", type: "human", content: "未完成请求" }];

    render(<App />);

    expect(screen.getByText("本轮未产生最终回复。")).toBeTruthy();
    expect(screen.getByText("未完成")).toBeTruthy();
  });

  it("详细模式选择不跨页面挂载持久化", () => {
    const view = render(<App />);
    enableDetailedMode();
    expectDetailedMode(true);

    view.unmount();
    render(<App />);
    expectDetailedMode(false);
  });

  it("注册后保存令牌、切换身份并清空旧 thread", async () => {
    streamState.values.async_tasks = {} as typeof streamState.values.async_tasks;
    const fetchMock = vi.fn().mockImplementation(async (input: string | URL | Request) => {
      if (String(input).endsWith("/auth/register")) {
        return {
          ok: true,
          json: async () => ({
            token: "token-a",
            user: { id: "user-a", username: "Alice" },
          }),
        };
      }
      if (String(input).endsWith("/auth/me")) {
        return {
          ok: true,
          json: async () => ({ user: { id: "user-a", username: "Alice" } }),
        };
      }
      return { ok: true, json: async () => [] };
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "注册" }));
    fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "Alice" } });
    fireEvent.change(screen.getByLabelText("密码"), { target: { value: "password8" } });
    fireEvent.change(screen.getByLabelText("确认密码"), { target: { value: "password8" } });
    fireEvent.click(screen.getByRole("button", { name: "注册并进入个人空间" }));

    await waitFor(() => expect(window.localStorage.getItem("deep-data-auth-token")).toBe("token-a"));
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:2024/auth/register",
      expect.objectContaining({ method: "POST" }),
    );
    expect(switchThread).not.toHaveBeenCalled();
    await waitFor(() => expect(capturedOptions?.defaultHeaders).toEqual({ Authorization: "Bearer token-a" }));
  });

  it("账户菜单通过设置弹窗清除当前用户记忆", async () => {
    streamState.values.async_tasks = {} as typeof streamState.values.async_tasks;
    window.localStorage.setItem("deep-data-auth-token", "token-a");
    const fetchMock = vi.fn().mockImplementation(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ user: { id: "user-a", username: "Alice" } }),
        };
      }
      if (url.endsWith("/memories/user") && init?.method === "DELETE") {
        return {
          ok: true,
          status: 200,
          json: async () => ({ status: "cleared", cancelled_jobs: 1 }),
        };
      }
      if (url.endsWith("/memories/settings")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ failure_lesson_saving_enabled: true }),
        };
      }
      return { ok: true, status: 200, json: async () => [] };
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    expect(await screen.findByText("Alice")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "打开账户菜单" }));
    expect(screen.getByRole("menuitem", { name: "退出登录" })).toBeTruthy();
    expect(screen.queryByText("注销")).toBeNull();
    fireEvent.click(screen.getByRole("menuitem", { name: "设置" }));
    expect(screen.getByRole("dialog", { name: "设置" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "清除记忆" }));
    expect(screen.getByText(/会话、文件、Skill 与公共失败经验不受影响/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "确认清除" }));

    expect(await screen.findByText("记忆已清除，将从下一次任务起生效。")).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:2024/memories/user",
      {
        method: "DELETE",
        headers: { Authorization: "Bearer token-a" },
      },
    );
  });

  it("未配置时阻止发送，并只把 API Key 提交到 Provider 保存接口", async () => {
    streamState.values.async_tasks = {} as typeof streamState.values.async_tasks;
    const secret = "sk-browser-memory-only";
    let savedBody: Record<string, unknown> | null = null;
    const fetchMock = vi.fn().mockImplementation(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ user: { id: "user-a", username: "Alice" } }),
        };
      }
      if (url.endsWith("/memories/settings")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ failure_lesson_saving_enabled: true }),
        };
      }
      if (url.endsWith("/model-provider") && init?.method === "PUT") {
        savedBody = JSON.parse(String(init.body)) as Record<string, unknown>;
        return {
          ok: true,
          status: 200,
          json: async () => ({
            configured: true,
            provider: {
              provider_name: "openai-compatible",
              provider_type: "chat_completions",
              base_url: "https://models.example.com/v1",
              model_name: "safe-model",
              has_api_key: true,
              api_key_hint: "only",
              version: 1,
              updated_at: "2026-08-28T10:00:00",
            },
          }),
        };
      }
      if (url.endsWith("/model-provider")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ configured: false, provider: null }),
        };
      }
      return {
        ok: true,
        status: 200,
        json: async () => (url.endsWith("/async-tasks/status") ? { tasks: [] } : []),
      };
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    await screen.findByText("尚未配置模型 Provider");
    expect((screen.getByRole("button", { name: "发送分析任务" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: "前往设置" }));
    expect(screen.queryByLabelText("Provider 类型")).toBeNull();
    fireEvent.change(screen.getByLabelText("API Base URL"), {
      target: { value: "https://models.example.com/v1" },
    });
    fireEvent.change(screen.getByLabelText("模型名"), { target: { value: "safe-model" } });
    fireEvent.change(screen.getByLabelText("API Key"), { target: { value: secret } });

    expect(JSON.stringify(window.localStorage)).not.toContain(secret);
    expect(JSON.stringify(window.sessionStorage)).not.toContain(secret);
    expect(JSON.stringify(capturedOptions)).not.toContain(secret);
    fireEvent.click(screen.getByRole("button", { name: "保存 Provider" }));

    await screen.findByText("模型 Provider 已保存");
    expect(savedBody).toEqual({
      base_url: "https://models.example.com/v1",
      model_name: "safe-model",
      api_key: secret,
    });
    expect((screen.getByLabelText("API Key") as HTMLInputElement).value).toBe("");
    expect(JSON.stringify(window.localStorage)).not.toContain(secret);
    expect(JSON.stringify(window.sessionStorage)).not.toContain(secret);
  });

  it("已保存 Key 可留空测试、更新和删除", async () => {
    streamState.values.async_tasks = {} as typeof streamState.values.async_tasks;
    const provider = {
      provider_name: "dashscope",
      provider_type: "chat_completions" as const,
      base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
      model_name: "qwen-max",
      has_api_key: true,
      api_key_hint: "1234",
      version: 2,
      updated_at: "2026-08-28T10:00:00",
    };
    const mutationBodies: Array<{ method: string; body?: Record<string, unknown> }> = [];
    vi.stubGlobal("fetch", withAuthenticatedUser(vi.fn().mockImplementation(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/model-provider/test")) {
        mutationBodies.push({ method: "POST", body: JSON.parse(String(init?.body)) as Record<string, unknown> });
        return {
          ok: true,
          status: 200,
          json: async () => ({
            ok: true,
            provider_name: "dashscope",
            provider_type: "chat_completions",
            latency_ms: 42,
          }),
        };
      }
      if (url.endsWith("/model-provider") && init?.method === "PUT") {
        mutationBodies.push({ method: "PUT", body: JSON.parse(String(init.body)) as Record<string, unknown> });
        return { ok: true, status: 200, json: async () => ({ configured: true, provider }) };
      }
      if (url.endsWith("/model-provider") && init?.method === "DELETE") {
        mutationBodies.push({ method: "DELETE" });
        return { ok: true, status: 200, json: async () => ({ configured: false, deleted: true }) };
      }
      return { ok: true, status: 200, json: async () => (url.endsWith("/async-tasks/status") ? { tasks: [] } : []) };
    })));
    render(<App />);

    await screen.findByText("Alice");
    fireEvent.click(screen.getByRole("button", { name: "打开账户菜单" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "设置" }));
    expect((screen.getByLabelText("API Key") as HTMLInputElement).placeholder).toContain("尾号 1234");
    fireEvent.change(screen.getByLabelText("模型名"), { target: { value: "qwen-max" } });
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    await screen.findByText("连接成功 · Chat Completions · 42 ms");
    await screen.findByText("已识别：Chat Completions · dashscope");
    fireEvent.click(screen.getByRole("button", { name: "保存 Provider" }));
    await screen.findByText("模型 Provider 已保存");
    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    await screen.findByText("模型 Provider 已删除");

    expect(mutationBodies).toEqual([
      {
        method: "POST",
        body: {
          base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
          model_name: "qwen-max",
        },
      },
      {
        method: "PUT",
        body: {
          base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
          model_name: "qwen-max",
        },
      },
      { method: "DELETE" },
    ]);
  });

  it("失败经验整理开关按服务端状态显示并允许运行中关闭", async () => {
    streamState.isLoading = true;
    let enabled = true;
    const fetchMock = vi.fn().mockImplementation(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ user: { id: "user-a", username: "Alice" } }),
        };
      }
      if (url.endsWith("/memories/settings") && init?.method === "PATCH") {
        enabled = (JSON.parse(String(init.body)) as { failure_lesson_saving_enabled: boolean }).failure_lesson_saving_enabled;
        return {
          ok: true,
          status: 200,
          json: async () => ({ failure_lesson_saving_enabled: enabled, cancelled_jobs: 1 }),
        };
      }
      if (url.endsWith("/memories/settings")) {
        return {
          ok: true,
          status: 200,
          json: async () => ({ failure_lesson_saving_enabled: enabled }),
        };
      }
      return { ok: true, status: 200, json: async () => [] };
    });
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    await screen.findByText("Alice");
    fireEvent.click(screen.getByRole("button", { name: "打开账户菜单" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "设置" }));
    const toggle = await screen.findByRole("switch", { name: "失败经验整理" });
    await waitFor(() => expect(toggle.getAttribute("aria-checked")).toBe("true"));
    expect((toggle as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(toggle);

    await waitFor(() => expect(toggle.getAttribute("aria-checked")).toBe("false"));
    expect(screen.getByText(/仍会使用已有公共经验/)).toBeTruthy();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:2024/memories/settings",
      expect.objectContaining({
        method: "PATCH",
        body: JSON.stringify({ failure_lesson_saving_enabled: false }),
      }),
    );
  });

  it("运行期间设置弹窗允许切换显示但禁止清除记忆", async () => {
    streamState.isLoading = true;
    render(<App />);
    await screen.findByText("Alice");

    fireEvent.click(screen.getByRole("button", { name: "打开账户菜单" }));
    fireEvent.click(screen.getByRole("menuitem", { name: "设置" }));

    expect((screen.getByRole("button", { name: "清除记忆" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByLabelText("API Base URL") as HTMLInputElement).disabled).toBe(true);
    expect(screen.getByText("当前任务结束后才能修改 Provider。")).toBeTruthy();
    expect(screen.getByText("当前任务结束后才能清除记忆。")).toBeTruthy();
    fireEvent.click(screen.getByRole("switch", { name: "详细模式" }));
    expect(screen.getByRole("switch", { name: "详细模式" }).getAttribute("aria-checked")).toBe("true");
  });

  it("列出当前用户的会话并可切换历史 thread", async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ([
        {
          thread_id: "thread-new",
          metadata: { kind: "conversation", title: "比较三家产品价格" },
          status: "idle",
          updated_at: "2026-08-01T00:10:00Z",
        },
        {
          thread_id: "thread-old",
          metadata: { kind: "conversation", title: "分析 Tavily 文档" },
          status: "idle",
          updated_at: "2026-07-31T23:10:00Z",
        },
      ]),
    });
    vi.stubGlobal("fetch", withAuthenticatedUser(fetchMock));
    render(<App />);

    expect(await screen.findByText("比较三家产品价格")).toBeTruthy();
    expect(screen.getAllByText("分析 Tavily 文档").length).toBeGreaterThan(0);
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:2024/threads/search",
      expect.objectContaining({ method: "POST" }),
    );
    const searchOptions = fetchMock.mock.calls[0]?.[1] as RequestInit;
    expect(JSON.parse(String(searchOptions.body))).toEqual(expect.objectContaining({
      metadata: { graph_id: "supervisor" },
      extract: { first_message: "values.messages[0].content" },
    }));

    fireEvent.click(screen.getByRole("button", { name: "打开会话：比较三家产品价格" }));
    expect(switchThread).not.toHaveBeenCalled();
    expect(capturedOptions?.threadId).toBe("thread-new");
    expect(window.location.search).toBe("?thread=thread-new");
  });

  it("运行中的会话可切到其他 thread，且不会取消原 run 或调用 switchThread", async () => {
    window.history.replaceState({}, "", "http://localhost:5174/?thread=thread-a");
    streamState.isLoading = true;
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ([
        { thread_id: "thread-a", metadata: { title: "后台任务 A" }, status: "busy" },
        { thread_id: "thread-b", metadata: { title: "会话 B" }, status: "idle" },
      ]),
    });
    vi.stubGlobal("fetch", withAuthenticatedUser(fetchMock));
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "打开会话：会话 B" }));

    expect(window.location.search).toBe("?thread=thread-b");
    expect(capturedOptions?.threadId).toBe("thread-b");
    expect(await screen.findByText(/后台运行/)).toBeTruthy();
    expect(cancelRun).not.toHaveBeenCalled();
    expect(stop).not.toHaveBeenCalled();
    expect(switchThread).not.toHaveBeenCalled();
  });

  it("同一个 run 多次离开并返回时可以再次 joinStream", async () => {
    window.history.replaceState({}, "", "http://localhost:5174/?thread=thread-a");
    const activeRun = {
      run_id: "run-a",
      thread_id: "thread-a",
      assistant_id: "supervisor",
      status: "running",
      created_at: "2026-08-22T09:00:00Z",
      updated_at: "2026-08-22T09:00:00Z",
      metadata: {},
      multitask_strategy: null,
    };
    listRuns.mockImplementation(async (thread, options) => (
      thread === "thread-a" && options.status === "running" ? [activeRun] : []
    ));
    joinStream.mockResolvedValue(undefined);
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ([
        { thread_id: "thread-a", metadata: { title: "任务 A" }, status: "busy" },
        { thread_id: "thread-b", metadata: { title: "任务 B" }, status: "idle" },
      ]),
    });
    vi.stubGlobal("fetch", withAuthenticatedUser(fetchMock));
    render(<App />);

    await waitFor(() => expect(joinStream).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "打开会话：任务 B" }));
    fireEvent.click(screen.getByRole("button", { name: "打开会话：任务 A" }));

    await waitFor(() => expect(joinStream).toHaveBeenCalledTimes(2));
    expect(joinStream.mock.calls).toEqual([["run-a"], ["run-a"]]);
    expect(switchThread).not.toHaveBeenCalled();
  });

  it("无需确认即可删除会话，删除当前会话后切换到新任务", async () => {
    streamState.values.async_tasks = {} as typeof streamState.values.async_tasks;
    window.history.replaceState({}, "", "http://localhost:5174/?thread=thread-delete");
    const fetchMock = vi.fn().mockImplementation(async (_input: string | URL | Request, init?: RequestInit) => {
      if (init?.method === "DELETE") {
        return { ok: true, status: 204 };
      }
      return {
        ok: true,
        json: async () => ([{
          thread_id: "thread-delete",
          metadata: { kind: "conversation", title: "待删除的采购会话" },
          status: "idle",
          updated_at: "2026-08-01T00:10:00Z",
        }]),
      };
    });
    vi.stubGlobal("fetch", withAuthenticatedUser(fetchMock));
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "删除会话：待删除的采购会话" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:2024/threads/thread-delete",
      expect.objectContaining({ method: "DELETE" }),
    ));
    expect(window.confirm).not.toHaveBeenCalled();
    expect(switchThread).not.toHaveBeenCalled();
    expect(capturedOptions?.threadId).toBeUndefined();
    expect(window.location.search).toBe("");
    expect(screen.queryByText("待删除的采购会话")).toBeNull();
  });

  it("删除 busy thread 时取消失败会保留会话且不发出删除请求", async () => {
    window.history.replaceState({}, "", "http://localhost:5174/?thread=thread-busy-delete");
    streamState.isLoading = true;
    const running = {
      run_id: "run-active",
      thread_id: "thread-busy-delete",
      assistant_id: "supervisor",
      status: "running",
      created_at: "2026-08-22T09:00:00Z",
      updated_at: "2026-08-22T09:00:00Z",
      metadata: {},
      multitask_strategy: null,
    };
    const pending = { ...running, run_id: "run-pending", status: "pending" };
    listRuns.mockImplementation(async (_threadId, options) => (
      options.status === "running" ? [running] : [pending]
    ));
    cancelRun.mockRejectedValue(new Error("cancel failed"));
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ([{
        thread_id: "thread-busy-delete",
        metadata: { title: "不可删除的运行会话" },
        status: "busy",
      }]),
    });
    vi.stubGlobal("fetch", withAuthenticatedUser(fetchMock));
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "删除会话：不可删除的运行会话" }));

    await waitFor(() => expect(cancelRun).toHaveBeenCalled());
    expect(window.confirm).toHaveBeenCalled();
    expect(fetchMock.mock.calls.some(([input, init]) => (
      String(input).endsWith("/threads/thread-busy-delete") && init?.method === "DELETE"
    ))).toBe(false);
    expect(screen.getByText("不可删除的运行会话")).toBeTruthy();
  });

  it("展示 DeepAgents 计划、异步任务和工具调用", async () => {
    render(<App />);
    await screen.findByText("Alice");
    enableDetailedMode();

    expect(screen.getByText("研究步骤")).toBeTruthy();
    expect(screen.getByText("Supervisor 入口就绪")).toBeTruthy();
    expect(screen.getByText("50%")).toBeTruthy();
    expect(screen.getByText("任务控制台")).toBeTruthy();
    expect(screen.getByText("运行中")).toBeTruthy();
    expect(screen.getByText("启动后台任务")).toBeTruthy();
    expect(screen.getByText("后台任务：1")).toBeTruthy();
  });

  it("展示 Supervisor 直接调用的 Skill 管理工具", () => {
    streamState.messages = [
      {
        id: "ai-skill",
        type: "ai",
        content: "",
        tool_calls: [
          {
            id: "call-skill",
            name: "assign_skill",
            args: { subagent_type: "unused-in-direct-tool-test" },
          },
        ],
      },
      {
        id: "tool-skill",
        type: "tool",
        tool_call_id: "call-skill",
        content: "status: assigned",
      },
    ];

    render(<App />);
    enableDetailedMode();

    expect(screen.getByText("分配 Skill")).toBeTruthy();
  });

  it("将 task 工具显示为同步子智能体调用", () => {
    streamState.messages = [
      {
        id: "ai-subagent",
        type: "ai",
        content: "",
        tool_calls: [
          {
            id: "call-subagent",
            name: "task",
            args: { subagent_type: "data-analyst" },
          },
        ],
      },
    ];
    const subagentTrace = {
      id: "call-subagent",
      status: "running",
      result: null,
      error: null,
      startedAt: new Date("2026-08-07T08:00:00Z"),
      completedAt: null,
      namespace: ["tools:call-subagent"],
      parentId: null,
      depth: 0,
      toolCall: {
        id: "call-subagent",
        name: "task",
        args: { subagent_type: "data-analyst", description: "分析上传的表格" },
      },
      values: {
        todos: [
          { content: "读取并检查数据", status: "completed" },
          { content: "计算统计指标", status: "in_progress" },
        ],
      },
      messages: [
        { type: "ai", content: "已确认数据结构，正在计算描述性统计。" },
      ],
      toolCalls: [{
        id: "child-call-1",
        call: { id: "child-call-1", name: "profile_table", args: { input: "/workspace/input/data.csv" } },
        result: null,
        state: "completed" as "pending" | "completed" | "error",
      }],
    };
    streamState.subagents.set("call-subagent", subagentTrace);
    streamState.subagents.set("call-subagent-2", {
      ...subagentTrace,
      id: "call-subagent-2",
      status: "running",
      toolCall: {
        id: "call-subagent-2",
        name: "task",
        args: { subagent_type: "analysis-reviewer", description: "复核数据库分析结果" },
      },
      values: {},
      messages: [],
      toolCalls: [{
        id: "child-plan-2",
        call: {
          id: "child-plan-2",
          name: "write_todos",
          args: {
            todos: [
              { content: "核对关键查询", status: "in_progress" },
              { content: "复核结论", status: "pending" },
            ],
          },
        },
        result: null,
        state: "completed" as "pending" | "completed" | "error",
      }],
    });

    const view = render(<App />);
    enableDetailedMode();

    expect(screen.getByText("调用同步子智能体")).toBeTruthy();
    expect(screen.getByLabelText("data-analyst 子智能体执行过程")).toBeTruthy();
    const planPanel = screen.getByLabelText("子智能体计划");
    const operationsRail = screen.getByLabelText("研究执行状态");
    const supervisorPlan = screen.getByRole("heading", { name: "研究步骤" }).closest("section");
    expect(operationsRail.contains(planPanel)).toBe(true);
    expect(Array.from(operationsRail.children).indexOf(supervisorPlan as Element))
      .toBeLessThan(Array.from(operationsRail.children).indexOf(planPanel));
    expect(planPanel.textContent).toContain("计算统计指标");
    expect(planPanel.textContent).toContain("核对关键查询");
    expect(planPanel.textContent).not.toContain("分析上传的表格");
    expect(planPanel.textContent).not.toContain("复核数据库分析结果");
    const planCards = screen.getAllByLabelText(/data-analyst 子智能体计划 · 调用/);
    expect(planCards).toHaveLength(1);
    expect(screen.getByLabelText(/analysis-reviewer 子智能体计划 · 调用/)).toBeTruthy();
    expect((planCards[0] as HTMLDetailsElement).open).toBe(true);
    fireEvent.click(planCards[0].querySelector("summary") as HTMLElement);
    expect((planCards[0] as HTMLDetailsElement).open).toBe(false);
    expect(screen.getByText("已确认数据结构，正在计算描述性统计。")).toBeTruthy();
    expect(screen.getByText("profile_table")).toBeTruthy();

    const taskDetails = screen.getByText("调用同步子智能体").closest("details") as HTMLDetailsElement;
    const childDetails = screen.getByText("profile_table").closest("details") as HTMLDetailsElement;
    expect(taskDetails.open).toBe(true);
    expect(childDetails.open).toBe(true);

    subagentTrace.toolCalls[0].state = "completed";
    // SDK 的完成快照可能清空子图 values，计划仍应使用流式期间的缓存。
    (subagentTrace as unknown as { values: Record<string, unknown> }).values = {};
    streamState.messages = [
      ...streamState.messages,
      { id: "tool-subagent", type: "tool", tool_call_id: "call-subagent", content: "analysis complete" },
    ];
    view.rerender(<App />);

    const completedPlan = screen.getAllByLabelText(/data-analyst 子智能体计划 · 调用/)[0] as HTMLDetailsElement;
    expect(completedPlan.open).toBe(false);
    expect(completedPlan.textContent).toContain("已完成");
    expect(screen.getByLabelText("子智能体计划").textContent).toContain("计算统计指标");
    expect(screen.getByLabelText("data-analyst 子智能体执行过程").textContent).toContain("已完成");
    expect((screen.getByText("调用同步子智能体").closest("details") as HTMLDetailsElement).open).toBe(true);
    expect((screen.getByText("profile_table").closest("details") as HTMLDetailsElement).open).toBe(true);
  });

  it("提交自然语言任务到 supervisor", async () => {
    streamState.messages = [];
    render(<App />);
    await screen.findByText("Alice");

    const input = screen.getByLabelText("描述你的网页或文件分析任务");
    fireEvent.change(input, { target: { value: "抓取官网并生成报告" } });
    fireEvent.click(screen.getByRole("button", { name: "发送分析任务" }));

    await waitFor(() => expect(submit).toHaveBeenCalledWith({
      messages: [{ type: "human", content: "抓取官网并生成报告" }],
    }, {
      metadata: {
        deep_data_ui: { submission_id: expect.any(String) },
        kind: "conversation",
        title: "抓取官网并生成报告",
      },
      optimisticValues: expect.any(Function),
      streamSubgraphs: true,
      streamResumable: true,
      onDisconnect: "continue",
    }));

    const optimisticValues = submit.mock.calls[0]?.[1]?.optimisticValues as
      | ((current: { messages: Array<{ type: string; content: string }> }) => {
          messages: Array<{ type: string; content: string }>;
        })
      | undefined;
    const optimisticState = optimisticValues?.({ messages: [] });
    expect(optimisticState?.messages).toEqual([expect.objectContaining({
      id: expect.stringMatching(/^optimistic-/),
      type: "human",
      content: "抓取官网并生成报告",
    })]);
  });

  it.each([
    [429, "QUESTION_RATE_LIMITED", "每分钟最多发起 20 次任务，请稍后再试", "本分钟提问次数已达上限"],
    [429, "TOKEN_BUDGET_EXHAUSTED", "Token 额度不足，请等待整点补充", "Token 额度不足"],
    [503, "RATE_LIMIT_SERVICE_UNAVAILABLE", "请求保护服务暂不可用，请稍后重试", "暂时无法发起任务"],
  ])("准入返回 %s 时弹窗且保留未发送输入", async (status, code, message, title) => {
    vi.stubGlobal("fetch", withAuthenticatedUser(vi.fn().mockImplementation(async (input: string | URL | Request) => {
      if (String(input).endsWith("/run-admissions")) {
        return {
          ok: false,
          status,
          json: async () => ({
            detail: {
              code,
              message,
              limit: status === 429 ? 20 : undefined,
              retry_after_seconds: status === 429 ? 17 : 0,
              active_thread_ids: [],
            },
          }),
        };
      }
      return { ok: true, status: 200, json: async () => [] };
    })));
    streamState.messages = [];
    render(<App />);
    await screen.findByText("Alice");

    const input = screen.getByLabelText("描述你的网页或文件分析任务");
    fireEvent.change(input, { target: { value: "这条消息不能丢" } });
    fireEvent.click(screen.getByRole("button", { name: "发送分析任务" }));

    expect(await screen.findByRole("dialog", { name: title })).toBeTruthy();
    expect((input as HTMLTextAreaElement).value).toBe("这条消息不能丢");
    expect(submit).not.toHaveBeenCalled();
  });

  it("3 个运行会话超限时列出并可跳转到自己的活跃会话", async () => {
    const activeThreadId = "11111111-1111-4111-8111-111111111111";
    vi.stubGlobal("fetch", withAuthenticatedUser(vi.fn().mockImplementation(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.endsWith("/run-admissions")) {
        return {
          ok: false,
          status: 409,
          json: async () => ({
            detail: {
              code: "THREAD_CONCURRENCY_LIMIT",
              message: "最多同时运行 3 个会话，请等待或停止其中一个",
              limit: 3,
              retry_after_seconds: 0,
              active_thread_ids: [activeThreadId],
            },
          }),
        };
      }
      if (url.endsWith("/threads/search")) {
        return {
          ok: true,
          status: 200,
          json: async () => [{
            thread_id: activeThreadId,
            status: "busy",
            metadata: { title: "正在分析订单" },
          }],
        };
      }
      return { ok: true, status: 200, json: async () => [] };
    })));
    streamState.messages = [];
    render(<App />);
    await screen.findByText("Alice");

    fireEvent.change(screen.getByLabelText("描述你的网页或文件分析任务"), {
      target: { value: "第四个会话" },
    });
    fireEvent.click(screen.getByRole("button", { name: "发送分析任务" }));

    const dialog = await screen.findByRole("dialog", { name: "运行中的会话已达上限" });
    fireEvent.click(within(dialog).getByRole("button", { name: new RegExp("正在分析订单") }));
    expect(new URLSearchParams(window.location.search).get("thread")).toBe(activeThreadId);
    expect(submit).not.toHaveBeenCalled();
  });

  it("运行中的内部子图空状态不会清空主对话", () => {
    streamState.messages = [
      { id: "human-stable", type: "human", content: "保留这条主对话" },
    ];
    const view = render(<App />);

    expect(screen.getByText("保留这条主对话")).toBeTruthy();

    streamState.messages = [];
    streamState.values = {
      context_usage: undefined,
      todos: [],
      async_tasks: {} as typeof streamState.values.async_tasks,
    };
    streamState.isLoading = true;
    view.rerender(<App />);

    expect(screen.getByText("保留这条主对话")).toBeTruthy();
    expect(screen.queryByText("把一个问题，变成一条证据链。")).toBeNull();
  });

  it("离开底部后停止自动跟随并提供回底部按钮", () => {
    let scrollTop = 0;
    let resizeCallback: ResizeObserverCallback | undefined;
    const scrollTo = vi.mocked(window.scrollTo);
    vi.spyOn(window, "scrollY", "get").mockImplementation(() => scrollTop);
    vi.spyOn(window, "innerHeight", "get").mockReturnValue(800);
    vi.spyOn(document.documentElement, "scrollHeight", "get").mockReturnValue(2_000);
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: ResizeObserverCallback) {
        resizeCallback = callback;
      }

      observe() {}
      unobserve() {}
      disconnect() {}
    });
    streamState.messages = [{ id: "human-scroll", type: "human", content: "滚动位置测试" }];
    const view = render(<App />);
    enableDetailedMode();

    act(() => window.dispatchEvent(new Event("scroll")));
    expect(screen.getByRole("button", { name: "回到对话底部" })).toBeTruthy();

    scrollTo.mockClear();
    streamState.messages = [
      ...streamState.messages,
      { id: "ai-scroll", type: "ai", content: "新增的流式内容" },
    ];
    view.rerender(<App />);
    act(() => resizeCallback?.([], {} as ResizeObserver));
    expect(scrollTo).not.toHaveBeenCalled();

    scrollTop = 1_200;
    fireEvent.click(screen.getByRole("button", { name: "回到对话底部" }));
    expect(scrollTo).toHaveBeenCalledWith({ top: 2_000, behavior: "auto" });

    scrollTo.mockClear();
    act(() => resizeCallback?.([], {} as ResizeObserver));
    expect(scrollTo).toHaveBeenCalledWith({ top: 2_000, behavior: "auto" });
  });

  it("空白会话选择文件后建 thread、顺序上传并把真实路径发给 Agent", async () => {
    streamState.messages = [];
    streamState.values.async_tasks = {} as typeof streamState.values.async_tasks;
    const threadId = "11111111-1111-4111-8111-111111111111";
    vi.spyOn(window.crypto, "randomUUID")
      .mockReturnValueOnce("22222222-2222-4222-8222-222222222222")
      .mockReturnValueOnce(threadId);
    const uploaded = {
      name: "orders.csv",
      path: "/workspace/input/orders.csv",
      size: 24,
      media_type: "text/csv",
    };
    const fetchMock = vi.fn().mockImplementation(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith("/threads/search")) return { ok: true, json: async () => [] };
      if (url.endsWith("/threads") && init?.method === "POST") {
        return { ok: true, json: async () => ({ thread_id: threadId }) };
      }
      if (url.endsWith(`/files/${threadId}`) && init?.method === "POST") {
        return { ok: true, json: async () => ({ files: [uploaded] }) };
      }
      if (url.endsWith(`/files/${threadId}`)) {
        return { ok: true, json: async () => ({ files: [uploaded] }) };
      }
      if (url.includes(`/artifacts/${threadId}`)) {
        return { ok: true, json: async () => ({ artifacts: [] }) };
      }
      return { ok: true, json: async () => [] };
    });
    vi.stubGlobal("fetch", withAuthenticatedUser(fetchMock));
    render(<App />);
    await screen.findByText("Alice");

    const file = new File(["id,amount\n001,10\n"], "orders.csv", { type: "text/csv" });
    fireEvent.change(screen.getByLabelText("选择本地表格文件"), {
      target: { files: [file] },
    });

    await waitFor(() => expect(screen.getByText(/已上传/)).toBeTruthy());
    expect(switchThread).not.toHaveBeenCalled();
    expect(capturedOptions?.threadId).toBe(threadId);
    expect(fetchMock).toHaveBeenCalledWith(
      `http://127.0.0.1:2024/files/${threadId}`,
      expect.objectContaining({ method: "POST", body: expect.any(FormData) }),
    );

    const input = screen.getByLabelText("描述你的网页或文件分析任务");
    fireEvent.change(input, { target: { value: "分析月度趋势" } });
    fireEvent.click(screen.getByRole("button", { name: "发送分析任务" }));

    await waitFor(() => expect(submit).toHaveBeenCalledWith({
      messages: [{
        type: "human",
        content: "分析月度趋势\n\n已上传文件：\n- /workspace/input/orders.csv",
      }],
    }, {
      metadata: { deep_data_ui: { submission_id: expect.any(String) } },
      optimisticValues: expect.any(Function),
      streamSubgraphs: true,
      streamResumable: true,
      onDisconnect: "continue",
    }));
  });

  it("刷新会话时恢复附件并可从服务端删除", async () => {
    streamState.values.async_tasks = {} as typeof streamState.values.async_tasks;
    window.history.replaceState({}, "", "http://localhost:5174/?thread=thread-files");
    let deleted = false;
    const fetchMock = vi.fn().mockImplementation(async (input: string | URL | Request, init?: RequestInit) => {
      const url = String(input);
      if (url.includes("/files/thread-files?") && init?.method === "DELETE") {
        deleted = true;
        return { ok: true, json: async () => ({ status: "deleted" }) };
      }
      if (url.endsWith("/files/thread-files")) {
        return {
          ok: true,
          json: async () => ({
            files: deleted ? [] : [{
              name: "history.xlsx",
              path: "/workspace/input/history.xlsx",
              size: 2048,
              media_type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            }],
          }),
        };
      }
      if (url.endsWith("/artifacts/thread-files")) {
        return { ok: true, json: async () => ({ artifacts: [] }) };
      }
      return { ok: true, json: async () => [] };
    });
    vi.stubGlobal("fetch", withAuthenticatedUser(fetchMock));
    render(<App />);

    expect(await screen.findByText("history.xlsx")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "删除附件：history.xlsx" }));

    await waitFor(() => expect(screen.queryByText("history.xlsx")).toBeNull());
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:2024/files/thread-files?path=%2Fworkspace%2Finput%2Fhistory.xlsx",
      { method: "DELETE", headers: {} },
    );
  });

  it("配置刷新恢复当前活动流", async () => {
    render(<App />);

    await screen.findByText("Alice");
    expect(capturedOptions?.reconnectOnMount).toBe(false);
    expect(capturedOptions?.throttle).toBe(false);
  });

  it("直接轮询后台任务状态而不启动 Supervisor run", async () => {
    const fetchMock = vi.fn().mockImplementation(async (input: string | URL | Request) => ({
      ok: true,
      json: async () => (String(input).endsWith("/async-tasks/status")
        ? { tasks: [{ ...streamState.values.async_tasks["task-123456789"], status: "running" }] }
        : []),
    }));
    vi.stubGlobal("fetch", withAuthenticatedUser(fetchMock));
    window.history.replaceState({}, "", "http://localhost:5174/?thread=parent-thread");

    render(<App />);

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:2024/async-tasks/status",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ thread_id: "parent-thread" }),
      }),
    ));
    expect(submit).not.toHaveBeenCalled();
  });

  it("任务完成后只启动一次 Supervisor run 自动读取结果", async () => {
    const fetchMock = vi.fn().mockImplementation(async (input: string | URL | Request) => ({
      ok: true,
      json: async () => (String(input).endsWith("/async-tasks/status")
        ? { tasks: [{ ...streamState.values.async_tasks["task-123456789"], status: "success" }] }
        : []),
    }));
    vi.stubGlobal("fetch", withAuthenticatedUser(fetchMock));
    window.history.replaceState({}, "", "http://localhost:5174/?thread=parent-thread");

    render(<App />);

    await waitFor(() => expect(submit).toHaveBeenCalledWith({
      messages: [{
        type: "human",
        name: "async-task-monitor",
        content: "后台任务 task-123456789 已完成。请调用 check_async_task 读取结果并继续处理，不要重新启动任务。",
      }],
    }, {
      metadata: { deep_data_ui: { submission_id: expect.any(String) } },
      streamSubgraphs: true,
      streamResumable: true,
      onDisconnect: "continue",
    }));
    expect(submit).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["error", "子任务发生内部类型错误，原样重试通常不会成功。", "执行失败"],
    ["timeout", "子任务执行超时，请缩小任务范围后重试。", "执行超时"],
    ["interrupted", "子任务已中断，需要恢复或重新发起。", "执行中断"],
  ] as const)("任务状态为 %s 时显示本地提醒且不启动模型", async (
    status,
    errorSummary,
    statusLabel,
  ) => {
    const fetchMock = vi.fn().mockImplementation(async (input: string | URL | Request) => ({
      ok: true,
      json: async () => (String(input).endsWith("/async-tasks/status")
        ? {
            tasks: [{
              ...streamState.values.async_tasks["task-123456789"],
              status,
              error_summary: errorSummary,
            }],
          }
        : []),
    }));
    vi.stubGlobal("fetch", withAuthenticatedUser(fetchMock));
    window.history.replaceState({}, "", "http://localhost:5174/?thread=parent-thread");

    render(<App />);

    await waitFor(() => expect(screen.getByText("有 1 个任务未正常完成")).toBeTruthy());
    // 简洁模式不挂载右侧任务卡，错误摘要由主区提醒保留一份即可。
    expect(screen.getAllByText(errorSummary).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(`crawl-worker · ${statusLabel}`)).toBeTruthy();
    expect(submit).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "关闭后台任务失败提醒" }));
    expect(screen.queryByText("有 1 个任务未正常完成")).toBeNull();
  });

  it("Supervisor 忙碌时保持输入可用并把普通消息排队", async () => {
    window.history.replaceState({}, "", "http://localhost:5174/?thread=thread-current");
    streamState.isLoading = true;
    render(<App />);
    await screen.findByText("Alice");

    const input = screen.getByLabelText("补充要求或纠正方向") as HTMLTextAreaElement;
    expect(input.disabled).toBe(false);

    fireEvent.change(input, { target: { value: "报告增加国内市场数据" } });
    fireEvent.click(screen.getByRole("button", { name: "排队发送消息" }));

    await waitFor(() => expect(createRun).toHaveBeenCalledWith(
      "thread-current",
      "supervisor",
      expect.objectContaining({
        input: { messages: [{ type: "human", content: "报告增加国内市场数据" }] },
        metadata: {
          deep_data_ui: {
            submission_id: expect.any(String),
            preview: "报告增加国内市场数据",
          },
        },
        multitaskStrategy: "enqueue",
        streamSubgraphs: true,
        streamResumable: true,
      }),
    ));
    expect(submit).not.toHaveBeenCalled();
  });

  it("可以立即纠正正在运行的 Supervisor", async () => {
    streamState.isLoading = true;
    render(<App />);
    await screen.findByText("Alice");

    fireEvent.change(screen.getByLabelText("补充要求或纠正方向"), {
      target: { value: "停止国外数据，只分析国内数据" },
    });
    fireEvent.click(screen.getByRole("button", { name: "立即纠正" }));

    await waitFor(() => expect(submit).toHaveBeenCalledWith({
      messages: [{ type: "human", content: "停止国外数据，只分析国内数据" }],
    }, {
      metadata: { deep_data_ui: { submission_id: expect.any(String) } },
      multitaskStrategy: "interrupt",
      optimisticValues: expect.any(Function),
      streamSubgraphs: true,
      streamResumable: true,
      onDisconnect: "continue",
    }));
  });

  it("任务卡通过 Supervisor 检查完整 task id", async () => {
    render(<App />);
    await screen.findByText("Alice");
    enableDetailedMode();

    fireEvent.click(screen.getByRole("button", { name: "检查进度" }));

    await waitFor(() => expect(submit).toHaveBeenCalledWith({
      messages: [{
        type: "human",
        content: "请调用 check_async_task 检查任务 task-123456789。如果远程运行已经结束，请读取结果第一行的业务 status 并继续处理。",
      }],
    }, {
      metadata: { deep_data_ui: { submission_id: expect.any(String) } },
      optimisticValues: expect.any(Function),
      streamSubgraphs: true,
      streamResumable: true,
      onDisconnect: "continue",
    }));
  });

  it("任务卡可以立即补充 crawl-worker 要求", async () => {
    streamState.isLoading = true;
    render(<App />);
    await screen.findByText("Alice");
    enableDetailedMode();

    fireEvent.click(screen.getByRole("button", { name: "补充要求" }));
    fireEvent.change(screen.getByLabelText("补充任务要求"), {
      target: { value: "只保留国内产品并增加价格字段" },
    });
    fireEvent.click(screen.getByRole("button", { name: "立即更新" }));

    await waitFor(() => expect(submit).toHaveBeenCalledWith({
      messages: [{
        type: "human",
        content: "请调用 update_async_task 更新任务 task-123456789。补充要求：只保留国内产品并增加价格字段",
      }],
    }, {
      metadata: { deep_data_ui: { submission_id: expect.any(String) } },
      multitaskStrategy: "interrupt",
      optimisticValues: expect.any(Function),
      streamSubgraphs: true,
      streamResumable: true,
      onDisconnect: "continue",
    }));
  });

  it("任务卡可以取消 crawl-worker", async () => {
    window.history.replaceState({}, "", "http://localhost:5174/?thread=thread-current");
    streamState.isLoading = true;
    render(<App />);
    await screen.findByText("Alice");
    enableDetailedMode();

    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(window.confirm).toHaveBeenCalled();
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledWith(
      "http://127.0.0.1:2024/async-tasks/task-123456789/cancel",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ thread_id: "thread-current" }),
      }),
    ));
    expect(submit).not.toHaveBeenCalled();
    expect(await screen.findByText("已取消")).toBeTruthy();
  });

  it("展示并管理 Supervisor 服务端等待队列", async () => {
    window.history.replaceState({}, "", "http://localhost:5174/?thread=thread-current");
    streamState.isLoading = true;
    const pendingIds = new Set<string>();
    const makePendingRun = (id: string) => ({
      run_id: id,
      thread_id: "thread-current",
      assistant_id: "supervisor",
      status: "pending",
      created_at: id.endsWith("1") ? "2026-07-28T12:00:00Z" : "2026-07-28T12:01:00Z",
      updated_at: "2026-07-28T12:01:00Z",
      metadata: {
        deep_data_ui: { preview: id.endsWith("1") ? "报告增加一张对比表" : "补充风险说明" },
      },
      multitask_strategy: "enqueue",
    });
    listRuns.mockImplementation(async (_threadId, options) => (
      options?.status === "pending" ? [...pendingIds].map(makePendingRun) : []
    ));
    createRun.mockImplementation(async () => {
      const id = pendingIds.size === 0 ? "queued-run-1" : "queued-run-2";
      pendingIds.add(id);
      return makePendingRun(id);
    });
    getRun.mockImplementation(async (_threadId, runId) => makePendingRun(runId));
    cancelRun.mockImplementation(async (_threadId, runId) => {
      pendingIds.delete(runId);
    });
    render(<App />);
    await screen.findByText("Alice");

    const input = screen.getByLabelText("补充要求或纠正方向");
    fireEvent.change(input, { target: { value: "报告增加一张对比表" } });
    fireEvent.click(screen.getByRole("button", { name: "排队发送消息" }));
    await waitFor(() => expect(createRun).toHaveBeenCalledTimes(1));
    fireEvent.change(input, { target: { value: "补充风险说明" } });
    fireEvent.click(screen.getByRole("button", { name: "排队发送消息" }));

    expect(await screen.findByText("等待处理 · 2")).toBeTruthy();
    expect(screen.getByText("报告增加一张对比表")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "撤销等待消息：报告增加一张对比表" }));
    await waitFor(() => expect(cancelRun).toHaveBeenCalledWith(
      "thread-current", "queued-run-1", true, "interrupt",
    ));

    fireEvent.click(screen.getByRole("button", { name: "清空等待消息" }));
    await waitFor(() => expect(cancelRun).toHaveBeenCalledWith(
      "thread-current", "queued-run-2", true, "interrupt",
    ));
    expect(cancelQueuedRun).not.toHaveBeenCalled();
    expect(clearQueue).not.toHaveBeenCalled();
  });

  it("把新 thread id 写入 URL", async () => {
    render(<App />);
    await screen.findByText("Alice");

    act(() => {
      (capturedOptions?.onThreadId as ((id: string) => void) | undefined)?.("thread-abc");
    });

    expect(window.location.search).toBe("?thread=thread-abc");
  });

  it("采购信息不足时提交 respond 恢复原任务", async () => {
    streamState.interrupt = {
      id: "interrupt-ask",
      value: {
        action_requests: [{
          name: "ask_user",
          args: {
            question: "本次采购数量是多少？",
            missing_fields: ["采购数量"],
            known_information: "型号为 A100",
          },
        }],
        review_configs: [{
          action_name: "ask_user",
          allowed_decisions: ["respond"],
        }],
      },
    };
    render(<App />);

    fireEvent.change(screen.getByPlaceholderText("请输入补充信息"), {
      target: { value: "采购 500 件，交付到上海" },
    });
    fireEvent.click(screen.getByRole("button", { name: "提交并继续" }));

    await waitFor(() => expect(submit).toHaveBeenCalledWith(null, {
      command: {
        resume: {
          decisions: [{ type: "respond", message: "采购 500 件，交付到上海" }],
        },
      },
      metadata: { deep_data_ui: { submission_id: expect.any(String) } },
      streamSubgraphs: true,
      streamResumable: true,
      onDisconnect: "continue",
    }));
    expect(
      (screen.getByLabelText("请先处理上方待确认事项") as HTMLTextAreaElement).disabled,
    ).toBe(true);
  });

  it("语义下载要求批准后恢复下载工具", async () => {
    window.history.replaceState({}, "", "http://localhost:5174/?thread=thread-a");
    const createObjectURL = vi.fn().mockReturnValue("blob:semantic-report");
    Object.defineProperty(window.URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(window.URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    vi.stubGlobal("fetch", withAuthenticatedUser(vi.fn().mockImplementation(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.includes("/artifacts/thread-a/bundle")) {
        return { ok: true, blob: async () => new Blob(["# 报告"]) };
      }
      if (url.endsWith("/artifacts/thread-a")) {
        return { ok: true, json: async () => ({ artifacts: [] }) };
      }
      return { ok: true, json: async () => [] };
    })));
    streamState.interrupt = {
      id: "interrupt-download",
      value: {
        action_requests: [{
          name: "request_report_download",
          args: { file_path: "/workspace/final_report.md" },
        }],
        review_configs: [{
          action_name: "request_report_download",
          allowed_decisions: ["approve", "reject"],
        }],
      },
    };
    const rendered = render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "批准下载" }));
    fireEvent.click(screen.getByRole("button", { name: "提交并继续" }));

    await waitFor(() => expect(submit).toHaveBeenCalledWith(null, {
      command: { resume: { decisions: [{ type: "approve" }] } },
      metadata: { deep_data_ui: { submission_id: expect.any(String) } },
      streamSubgraphs: true,
      streamResumable: true,
      onDisconnect: "continue",
    }));

    streamState.interrupt = undefined;
    const mutableStream = streamState as unknown as {
      messages: Array<Record<string, unknown>>;
    };
    mutableStream.messages = [
      ...mutableStream.messages,
      {
        id: "tool-download",
        type: "tool",
        tool_call_id: "call-download",
        content: "文件已准备下载",
        artifact: {
          type: "file_download",
          path: "/workspace/final_report.md",
          filename: "final_report.md",
          size: 8,
        },
      },
    ];
    rendered.rerender(<App />);

    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
  });

  it("邮件发送在简略模式展示完整确认信息并经批准恢复", async () => {
    streamState.interrupt = {
      id: "interrupt-email",
      value: {
        action_requests: [{
          name: "send_report_email",
          args: {
            recipient: "reader@example.com",
            subject: "月度物流分析",
            pdf_path: "/workspace/output/logistics.pdf",
            markdown_path: "/workspace/output/logistics.md",
          },
        }],
        review_configs: [{
          action_name: "send_report_email",
          allowed_decisions: ["approve", "reject"],
        }],
      },
    };
    render(<App />);

    expect(screen.getByText("是否确认发送报告邮件？")).toBeTruthy();
    expect(screen.getByText("reader@example.com")).toBeTruthy();
    expect(screen.getByText("月度物流分析")).toBeTruthy();
    expect(screen.getByText("logistics.pdf")).toBeTruthy();
    expect(screen.getByText("logistics-bundle.zip")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "确认发送" }));
    fireEvent.click(screen.getByRole("button", { name: "提交并继续" }));

    await waitFor(() => expect(submit).toHaveBeenCalledWith(null, {
      command: { resume: { decisions: [{ type: "approve" }] } },
      metadata: { deep_data_ui: { submission_id: expect.any(String) } },
      streamSubgraphs: true,
      streamResumable: true,
      onDisconnect: "continue",
    }));
  });

  it("取消邮件发送会以 reject 恢复且不执行原工具", async () => {
    streamState.interrupt = {
      id: "interrupt-email-reject",
      value: {
        action_requests: [{
          name: "send_report_email",
          args: { recipient: "reader@example.com" },
        }],
        review_configs: [{
          action_name: "send_report_email",
          allowed_decisions: ["approve", "reject"],
        }],
      },
    };
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "取消发送" }));
    fireEvent.click(screen.getByRole("button", { name: "提交并继续" }));

    await waitFor(() => expect(submit).toHaveBeenCalledWith(null, {
      command: {
        resume: {
          decisions: [{ type: "reject", message: "用户拒绝执行该操作。" }],
        },
      },
      metadata: { deep_data_ui: { submission_id: expect.any(String) } },
      streamSubgraphs: true,
      streamResumable: true,
      onDisconnect: "continue",
    }));
  });

  it("研究产物按钮通过鉴权接口下载文件", async () => {
    window.history.replaceState({}, "", "http://localhost:5174/?thread=thread-a");
    const createObjectURL = vi.fn().mockReturnValue("blob:report");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(window.URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(window.URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const fetchMock = vi.fn().mockImplementation(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.includes("/artifacts/thread-a/bundle")) {
        return { ok: true, blob: async () => new Blob(["# 报告"]) };
      }
      if (url.endsWith("/artifacts/thread-a")) {
        return {
          ok: true,
          json: async () => ({
            artifacts: [{
              path: "/workspace/final_report.md",
              filename: "final_report.md",
              size: 8,
              mime_type: "text/markdown",
            }],
          }),
        };
      }
      return { ok: true, json: async () => [] };
    });
    vi.stubGlobal("fetch", withAuthenticatedUser(fetchMock));
    render(<App />);

    await waitFor(() => expect(screen.getByText("final_report.md")).toBeTruthy());
    expect(screen.queryByRole("button", { name: "下载 MD" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "下载 ZIP" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:2024/artifacts/thread-a/bundle?path=%2Fworkspace%2Ffinal_report.md",
      { headers: {} },
    ));
    await waitFor(() => expect(createObjectURL).toHaveBeenCalled());
    expect(anchorClick).toHaveBeenCalled();
  });

  it("Markdown 相对图片通过当前会话鉴权接口加载", async () => {
    window.history.replaceState({}, "", "http://localhost:5174/?thread=thread-a");
    const createObjectURL = vi.fn().mockReturnValue("blob:chart-image");
    Object.defineProperty(window.URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(window.URL, "revokeObjectURL", { configurable: true, value: vi.fn() });
    const fetchMock = vi.fn().mockImplementation(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.includes("/artifacts/thread-a/download?path=%2Fworkspace%2Foutput%2Fcharts%2Fprice.png")) {
        return { ok: true, blob: async () => new Blob(["png"]) };
      }
      if (url.endsWith("/artifacts/thread-a")) {
        return { ok: true, json: async () => ({ artifacts: [] }) };
      }
      return { ok: true, json: async () => [] };
    });
    vi.stubGlobal("fetch", withAuthenticatedUser(fetchMock));
    streamState.messages = [
      { id: "human-chart", type: "human", content: "显示图表" },
      { id: "ai-chart", type: "ai", content: "![价格对比](charts/price.png)" },
    ];

    render(<App />);

    await waitFor(() => expect(screen.getByAltText("价格对比").getAttribute("src")).toBe("blob:chart-image"));
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:2024/artifacts/thread-a/download?path=%2Fworkspace%2Foutput%2Fcharts%2Fprice.png",
      { headers: {}, signal: expect.any(AbortSignal) },
    );
  });

  it("开始新任务时清空草稿并切换到空 thread", async () => {
    render(<App />);
    await screen.findByText("Alice");

    const input = screen.getByLabelText("描述你的网页或文件分析任务") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "尚未提交的研究任务" } });
    fireEvent.click(screen.getByRole("button", { name: "开始新任务" }));

    expect(window.confirm).not.toHaveBeenCalled();
    expect(switchThread).not.toHaveBeenCalled();
    expect(capturedOptions?.threadId).toBeUndefined();
    expect(input.value).toBe("");
  });
});
