import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "./App";

const submit = vi.fn();
const stop = vi.fn();
const switchThread = vi.fn();
const cancelQueuedRun = vi.fn();
const clearQueue = vi.fn();
let capturedOptions: Record<string, unknown> | null = null;

function createStreamState() {
  return {
    values: {
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
    ],
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

vi.mock("@langchain/react", () => ({
  useStream: (options: Record<string, unknown>) => {
    capturedOptions = options;
    return streamState;
  },
}));

beforeEach(() => {
  streamState = createStreamState();
  window.localStorage.clear();
  window.sessionStorage.clear();
  vi.spyOn(window, "confirm").mockReturnValue(true);
  vi.spyOn(window, "scrollTo").mockImplementation(() => undefined);
  vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: string | URL | Request) => ({
    ok: true,
    json: async () => (String(input).endsWith("/async-tasks/status") ? { tasks: [] } : []),
  })));
});

afterEach(() => {
  cleanup();
  submit.mockReset();
  stop.mockReset();
  switchThread.mockReset();
  cancelQueuedRun.mockReset();
  clearQueue.mockReset();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  capturedOptions = null;
  window.history.replaceState({}, "", "http://localhost:5174/");
  window.localStorage.clear();
  window.sessionStorage.clear();
});

describe("研究工作台", () => {
  it("未登录时使用默认账户且不发送认证头", () => {
    render(<App />);

    expect(screen.getByText("默认账户")).toBeTruthy();
    expect(capturedOptions?.defaultHeaders).toEqual({});
    expect(capturedOptions?.filterSubagentMessages).toBe(true);
    expect(capturedOptions?.onFinish).toEqual(expect.any(Function));
    expect((capturedOptions?.onFinish as ((state: unknown) => void)).length).toBe(1);
  });

  it("注册后保存令牌、切换身份并清空旧 thread", async () => {
    streamState.values.async_tasks = {} as typeof streamState.values.async_tasks;
    const fetchMock = vi.fn().mockImplementation(async (input: string | URL | Request) => {
      if (String(input).endsWith("/auth/register")) {
        return {
          ok: true,
          json: async () => ({
            token: "token-a",
            user: { id: "user-a", username: "Alice", is_default: false },
          }),
        };
      }
      if (String(input).endsWith("/auth/me")) {
        return {
          ok: true,
          json: async () => ({ user: { id: "user-a", username: "Alice", is_default: false } }),
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
    expect(switchThread).toHaveBeenCalledWith(null);
    await waitFor(() => expect(capturedOptions?.defaultHeaders).toEqual({ Authorization: "Bearer token-a" }));
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
    vi.stubGlobal("fetch", fetchMock);
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
    expect(switchThread).toHaveBeenCalledWith("thread-new");
    expect(window.location.search).toBe("?thread=thread-new");
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
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "删除会话：待删除的采购会话" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:2024/threads/thread-delete",
      expect.objectContaining({ method: "DELETE" }),
    ));
    expect(window.confirm).not.toHaveBeenCalled();
    expect(switchThread).toHaveBeenCalledWith(null);
    expect(window.location.search).toBe("");
    expect(screen.queryByText("待删除的采购会话")).toBeNull();
  });

  it("展示 DeepAgents 计划、异步任务和工具调用", () => {
    render(<App />);

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
        args: { subagent_type: "data-analyst", description: "复核数据库分析结果" },
      },
      values: {
        todos: [
          { content: "核对关键查询", status: "in_progress" },
          { content: "复核结论", status: "pending" },
        ],
      },
      messages: [],
      toolCalls: [],
    });

    const view = render(<App />);

    expect(screen.getByText("调用同步子智能体")).toBeTruthy();
    expect(screen.getByLabelText("data-analyst 子智能体执行过程")).toBeTruthy();
    const planPanel = screen.getByLabelText("子智能体计划");
    const operationsRail = screen.getByLabelText("研究执行状态");
    const supervisorPlan = screen.getByRole("heading", { name: "研究步骤" }).closest("section");
    expect(operationsRail.contains(planPanel)).toBe(true);
    expect(Array.from(operationsRail.children).indexOf(supervisorPlan as Element))
      .toBeLessThan(Array.from(operationsRail.children).indexOf(planPanel));
    expect(planPanel.textContent).toContain("计算统计指标");
    expect(planPanel.textContent).toContain("复核数据库分析结果");
    const planCards = screen.getAllByLabelText(/data-analyst 子智能体计划 · 调用/);
    expect(planCards).toHaveLength(2);
    expect((planCards[0] as HTMLDetailsElement).open).toBe(true);
    fireEvent.click(planCards[0].querySelector("summary") as HTMLElement);
    expect((planCards[0] as HTMLDetailsElement).open).toBe(false);
    expect(screen.getByText("已确认数据结构，正在计算描述性统计。")).toBeTruthy();
    expect(screen.getByText("profile_table")).toBeTruthy();

    const taskDetails = screen.getByText("调用同步子智能体").closest("details") as HTMLDetailsElement;
    const childDetails = screen.getByText("profile_table").closest("details") as HTMLDetailsElement;
    expect(taskDetails.open).toBe(true);
    expect(childDetails.open).toBe(true);

    subagentTrace.status = "complete";
    subagentTrace.toolCalls[0].state = "completed";
    streamState.messages = [
      ...streamState.messages,
      { id: "tool-subagent", type: "tool", tool_call_id: "call-subagent", content: "analysis complete" },
    ];
    view.rerender(<App />);

    expect((screen.getAllByLabelText(/data-analyst 子智能体计划 · 调用/)[0] as HTMLDetailsElement).open).toBe(false);
    expect((screen.getByText("调用同步子智能体").closest("details") as HTMLDetailsElement).open).toBe(true);
    expect((screen.getByText("profile_table").closest("details") as HTMLDetailsElement).open).toBe(true);
  });

  it("提交自然语言任务到 supervisor", () => {
    streamState.messages = [];
    render(<App />);

    const input = screen.getByLabelText("描述你的网页或文件分析任务");
    fireEvent.change(input, { target: { value: "抓取官网并生成报告" } });
    fireEvent.click(screen.getByRole("button", { name: "发送分析任务" }));

    expect(submit).toHaveBeenCalledWith({
      messages: [{ type: "human", content: "抓取官网并生成报告" }],
    }, {
      metadata: {
        kind: "conversation",
        title: "抓取官网并生成报告",
      },
      optimisticValues: expect.any(Function),
      streamSubgraphs: true,
      streamResumable: true,
      onDisconnect: "continue",
    });

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

  it("运行中的内部子图空状态不会清空主对话", () => {
    streamState.messages = [
      { id: "human-stable", type: "human", content: "保留这条主对话" },
    ];
    const view = render(<App />);

    expect(screen.getByText("保留这条主对话")).toBeTruthy();

    streamState.messages = [];
    streamState.values = {
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
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    const file = new File(["id,amount\n001,10\n"], "orders.csv", { type: "text/csv" });
    fireEvent.change(screen.getByLabelText("选择本地表格文件"), {
      target: { files: [file] },
    });

    await waitFor(() => expect(screen.getByText(/已上传/)).toBeTruthy());
    expect(switchThread).toHaveBeenCalledWith(threadId);
    expect(fetchMock).toHaveBeenCalledWith(
      `http://127.0.0.1:2024/files/${threadId}`,
      expect.objectContaining({ method: "POST", body: expect.any(FormData) }),
    );

    const input = screen.getByLabelText("描述你的网页或文件分析任务");
    fireEvent.change(input, { target: { value: "分析月度趋势" } });
    fireEvent.click(screen.getByRole("button", { name: "发送分析任务" }));

    expect(submit).toHaveBeenCalledWith({
      messages: [{
        type: "human",
        content: "分析月度趋势\n\n已上传文件：\n- /workspace/input/orders.csv",
      }],
    }, {
      optimisticValues: expect.any(Function),
      streamSubgraphs: true,
      streamResumable: true,
      onDisconnect: "continue",
    });
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
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    expect(await screen.findByText("history.xlsx")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "删除附件：history.xlsx" }));

    await waitFor(() => expect(screen.queryByText("history.xlsx")).toBeNull());
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:2024/files/thread-files?path=%2Fworkspace%2Finput%2Fhistory.xlsx",
      { method: "DELETE", headers: {} },
    );
  });

  it("配置刷新恢复当前活动流", () => {
    render(<App />);

    expect(capturedOptions?.reconnectOnMount).toBe(true);
    expect(capturedOptions?.throttle).toBe(60);
  });

  it("直接轮询后台任务状态而不启动 Supervisor run", async () => {
    const fetchMock = vi.fn().mockImplementation(async (input: string | URL | Request) => ({
      ok: true,
      json: async () => (String(input).endsWith("/async-tasks/status")
        ? { tasks: [{ ...streamState.values.async_tasks["task-123456789"], status: "running" }] }
        : []),
    }));
    vi.stubGlobal("fetch", fetchMock);
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
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", "http://localhost:5174/?thread=parent-thread");

    render(<App />);

    await waitFor(() => expect(submit).toHaveBeenCalledWith({
      messages: [{
        type: "human",
        name: "async-task-monitor",
        content: "后台任务 task-123456789 已完成。请调用 check_async_task 读取结果并继续处理，不要重新启动任务。",
      }],
    }, {
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
    vi.stubGlobal("fetch", fetchMock);
    window.history.replaceState({}, "", "http://localhost:5174/?thread=parent-thread");

    render(<App />);

    await waitFor(() => expect(screen.getByText("有 1 个任务未正常完成")).toBeTruthy());
    expect(screen.getAllByText(errorSummary).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText(`crawl-worker · ${statusLabel}`)).toBeTruthy();
    expect(submit).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "关闭后台任务失败提醒" }));
    expect(screen.queryByText("有 1 个任务未正常完成")).toBeNull();
  });

  it("Supervisor 忙碌时保持输入可用并把普通消息排队", () => {
    streamState.isLoading = true;
    render(<App />);

    const input = screen.getByLabelText("补充要求或纠正方向") as HTMLTextAreaElement;
    expect(input.disabled).toBe(false);

    fireEvent.change(input, { target: { value: "报告增加国内市场数据" } });
    fireEvent.click(screen.getByRole("button", { name: "排队发送消息" }));

    expect(submit).toHaveBeenCalledWith({
      messages: [{ type: "human", content: "报告增加国内市场数据" }],
    }, {
      multitaskStrategy: "enqueue",
      optimisticValues: expect.any(Function),
      streamSubgraphs: true,
      streamResumable: true,
      onDisconnect: "continue",
    });
  });

  it("可以立即纠正正在运行的 Supervisor", () => {
    streamState.isLoading = true;
    render(<App />);

    fireEvent.change(screen.getByLabelText("补充要求或纠正方向"), {
      target: { value: "停止国外数据，只分析国内数据" },
    });
    fireEvent.click(screen.getByRole("button", { name: "立即纠正" }));

    expect(submit).toHaveBeenCalledWith({
      messages: [{ type: "human", content: "停止国外数据，只分析国内数据" }],
    }, {
      multitaskStrategy: "interrupt",
      optimisticValues: expect.any(Function),
      streamSubgraphs: true,
      streamResumable: true,
      onDisconnect: "continue",
    });
  });

  it("任务卡通过 Supervisor 检查完整 task id", () => {
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "检查进度" }));

    expect(submit).toHaveBeenCalledWith({
      messages: [{
        type: "human",
        content: "请调用 check_async_task 检查任务 task-123456789。如果远程运行已经结束，请读取结果第一行的业务 status 并继续处理。",
      }],
    }, {
      optimisticValues: expect.any(Function),
      streamSubgraphs: true,
      streamResumable: true,
      onDisconnect: "continue",
    });
  });

  it("任务卡可以立即补充 crawl-worker 要求", () => {
    streamState.isLoading = true;
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "补充要求" }));
    fireEvent.change(screen.getByLabelText("补充任务要求"), {
      target: { value: "只保留国内产品并增加价格字段" },
    });
    fireEvent.click(screen.getByRole("button", { name: "立即更新" }));

    expect(submit).toHaveBeenCalledWith({
      messages: [{
        type: "human",
        content: "请调用 update_async_task 更新任务 task-123456789。补充要求：只保留国内产品并增加价格字段",
      }],
    }, {
      multitaskStrategy: "interrupt",
      optimisticValues: expect.any(Function),
      streamSubgraphs: true,
      streamResumable: true,
      onDisconnect: "continue",
    });
  });

  it("任务卡可以取消 crawl-worker", () => {
    streamState.isLoading = true;
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    expect(window.confirm).toHaveBeenCalled();
    expect(submit).toHaveBeenCalledWith({
      messages: [{
        type: "human",
        content: "请调用 cancel_async_task 取消任务 task-123456789。",
      }],
    }, {
      multitaskStrategy: "interrupt",
      optimisticValues: expect.any(Function),
      streamSubgraphs: true,
      streamResumable: true,
      onDisconnect: "continue",
    });
  });

  it("展示并管理 Supervisor 服务端等待队列", () => {
    streamState.queue.entries = [{
      id: "queued-run-1",
      values: {
        messages: [{ type: "human", content: "报告增加一张对比表" }],
      },
      createdAt: new Date("2026-07-28T12:00:00Z"),
    }];
    streamState.queue.size = 1;
    render(<App />);

    expect(screen.getByText("等待处理 · 1")).toBeTruthy();
    expect(screen.getByText("报告增加一张对比表")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "撤销等待消息：报告增加一张对比表" }));
    expect(cancelQueuedRun).toHaveBeenCalledWith("queued-run-1");

    fireEvent.click(screen.getByRole("button", { name: "清空等待消息" }));
    expect(clearQueue).toHaveBeenCalled();
  });

  it("把新 thread id 写入 URL", () => {
    render(<App />);

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
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.includes("/artifacts/thread-a/bundle")) {
        return { ok: true, blob: async () => new Blob(["# 报告"]) };
      }
      if (url.endsWith("/artifacts/thread-a")) {
        return { ok: true, json: async () => ({ artifacts: [] }) };
      }
      return { ok: true, json: async () => [] };
    }));
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

  it("研究产物按钮通过鉴权接口下载文件", async () => {
    window.history.replaceState({}, "", "http://localhost:5174/?thread=thread-a");
    const createObjectURL = vi.fn().mockReturnValue("blob:report");
    const revokeObjectURL = vi.fn();
    Object.defineProperty(window.URL, "createObjectURL", { configurable: true, value: createObjectURL });
    Object.defineProperty(window.URL, "revokeObjectURL", { configurable: true, value: revokeObjectURL });
    const anchorClick = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const fetchMock = vi.fn().mockImplementation(async (input: string | URL | Request) => {
      const url = String(input);
      if (url.includes("/artifacts/thread-a/download")) {
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
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);

    await waitFor(() => expect(screen.getByText("final_report.md")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "下载 MD" }));

    await waitFor(() => expect(createObjectURL).toHaveBeenCalled());
    expect(anchorClick).toHaveBeenCalled();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:2024/artifacts/thread-a/download?path=%2Fworkspace%2Ffinal_report.md",
      { headers: {} },
    );

    fireEvent.click(screen.getByRole("button", { name: "下载含图片 ZIP" }));
    await waitFor(() => expect(fetchMock).toHaveBeenCalledWith(
      "http://127.0.0.1:2024/artifacts/thread-a/bundle?path=%2Fworkspace%2Ffinal_report.md",
      { headers: {} },
    ));
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
    vi.stubGlobal("fetch", fetchMock);
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

  it("开始新任务时清空草稿并切换到空 thread", () => {
    render(<App />);

    const input = screen.getByLabelText("描述你的网页或文件分析任务") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "尚未提交的研究任务" } });
    fireEvent.click(screen.getByRole("button", { name: "开始新任务" }));

    expect(window.confirm).toHaveBeenCalled();
    expect(switchThread).toHaveBeenCalledWith(null);
    expect(input.value).toBe("");
  });
});
