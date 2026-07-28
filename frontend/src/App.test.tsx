import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
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
  vi.spyOn(window, "confirm").mockReturnValue(true);
});

afterEach(() => {
  cleanup();
  submit.mockReset();
  stop.mockReset();
  switchThread.mockReset();
  cancelQueuedRun.mockReset();
  clearQueue.mockReset();
  vi.restoreAllMocks();
  capturedOptions = null;
  window.history.replaceState({}, "", "http://localhost:5174/");
});

describe("研究工作台", () => {
  it("展示 DeepAgents 计划、异步任务和工具调用", () => {
    render(<App />);

    expect(screen.getByText("研究步骤")).toBeTruthy();
    expect(screen.getByText("Supervisor 入口就绪")).toBeTruthy();
    expect(screen.getByText("50%")).toBeTruthy();
    expect(screen.getByText("任务控制台")).toBeTruthy();
    expect(screen.getByText("采集中")).toBeTruthy();
    expect(screen.getByText("启动网页采集")).toBeTruthy();
    expect(screen.getByText("后台采集：1")).toBeTruthy();
  });

  it("提交自然语言任务到 supervisor", () => {
    render(<App />);

    const input = screen.getByLabelText("描述你的网页数据任务");
    fireEvent.change(input, { target: { value: "抓取官网并生成报告" } });
    fireEvent.click(screen.getByRole("button", { name: "发送研究任务" }));

    expect(submit).toHaveBeenCalledWith({
      messages: [{ type: "human", content: "抓取官网并生成报告" }],
    }, {
      streamResumable: true,
      onDisconnect: "continue",
    });
  });

  it("配置刷新恢复当前活动流", () => {
    render(<App />);

    expect(capturedOptions?.reconnectOnMount).toBe(true);
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
        content: "请调用 check_async_task 检查任务 task-123456789。如果任务已经完成，请读取最新结果并继续生成分析报告。",
      }],
    }, {
      streamResumable: true,
      onDisconnect: "continue",
    });
  });

  it("任务卡可以立即补充 crawl-worker 要求", () => {
    streamState.isLoading = true;
    render(<App />);

    fireEvent.click(screen.getByRole("button", { name: "补充要求" }));
    fireEvent.change(screen.getByLabelText("补充采集要求"), {
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

  it("开始新任务时清空草稿并切换到空 thread", () => {
    render(<App />);

    const input = screen.getByLabelText("描述你的网页数据任务") as HTMLTextAreaElement;
    fireEvent.change(input, { target: { value: "尚未提交的研究任务" } });
    fireEvent.click(screen.getByRole("button", { name: "开始新任务" }));

    expect(window.confirm).toHaveBeenCalled();
    expect(switchThread).toHaveBeenCalledWith(null);
    expect(input.value).toBe("");
  });
});
