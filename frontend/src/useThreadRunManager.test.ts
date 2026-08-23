import { act, renderHook, waitFor } from "@testing-library/react";
import type { Client } from "@langchain/langgraph-sdk/client";
import type { Run } from "@langchain/langgraph-sdk";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useThreadRunManager } from "./useThreadRunManager";

function makeRun(
  runId: string,
  threadId: string,
  status: Run["status"],
  createdAt: string,
  preview?: string,
): Run {
  return {
    run_id: runId,
    thread_id: threadId,
    assistant_id: "supervisor",
    status,
    created_at: createdAt,
    updated_at: createdAt,
    metadata: preview ? { deep_data_ui: { preview } } : {},
    multitask_strategy: "enqueue",
  } as Run;
}

describe("useThreadRunManager", () => {
  const list = vi.fn();
  const create = vi.fn();
  const get = vi.fn();
  const cancel = vi.fn();
  const client = {
    runs: { list, create, get, cancel },
  } as unknown as Client;

  beforeEach(() => {
    list.mockReset().mockResolvedValue([]);
    create.mockReset();
    get.mockReset();
    cancel.mockReset().mockResolvedValue(undefined);
  });

  it("按 thread 隔离排队任务并写入可恢复的 UI 元数据", async () => {
    create
      .mockResolvedValueOnce(makeRun("run-a", "thread-a", "pending", "2026-08-22T09:00:00Z"))
      .mockResolvedValueOnce(makeRun("run-b", "thread-b", "pending", "2026-08-22T09:01:00Z"));
    const { result } = renderHook(() => useThreadRunManager(client, "supervisor"));

    await act(async () => {
      await result.current.enqueue(
        "thread-a",
        { messages: [{ type: "human", content: "A 的补充要求" }] },
        `  A 的补充要求  ${"很长".repeat(100)}`,
      );
      await result.current.enqueue(
        "thread-b",
        { messages: [{ type: "human", content: "B 的补充要求" }] },
        "B 的补充要求",
      );
    });

    expect(result.current.runtimes["thread-a"].queuedRuns.map((entry) => entry.id)).toEqual(["run-a"]);
    expect(result.current.runtimes["thread-b"].queuedRuns.map((entry) => entry.id)).toEqual(["run-b"]);
    expect(create).toHaveBeenCalledWith(
      "thread-a",
      "supervisor",
      expect.objectContaining({
        multitaskStrategy: "enqueue",
        streamResumable: true,
        streamSubgraphs: true,
        metadata: {
          deep_data_ui: {
            submission_id: expect.any(String),
            preview: expect.any(String),
          },
        },
      }),
    );
    const options = create.mock.calls[0][2];
    expect(options.metadata.deep_data_ui.preview.length).toBeLessThanOrEqual(160);
  });

  it("以服务端状态恢复 active run，并按创建时间排列 pending run", async () => {
    const running = makeRun("run-active", "thread-a", "running", "2026-08-22T09:02:00Z");
    const pendingEarly = makeRun("run-early", "thread-a", "pending", "2026-08-22T09:03:00Z", "先执行");
    const pendingLate = makeRun("run-late", "thread-a", "pending", "2026-08-22T09:04:00Z", "后执行");
    list.mockImplementation(async (_threadId, options) => (
      options.status === "running" ? [running] : [pendingLate, pendingEarly]
    ));
    const { result } = renderHook(() => useThreadRunManager(client, "supervisor"));

    let active;
    await act(async () => {
      active = await result.current.reconcile("thread-a");
    });

    expect(active).toEqual({ running, pending: [pendingEarly, pendingLate] });
    expect(result.current.runtimes["thread-a"]).toMatchObject({
      activeRunId: "run-active",
      connection: "detached",
    });
    expect(result.current.runtimes["thread-a"].queuedRuns.map((entry) => entry.preview)).toEqual([
      "先执行",
      "后执行",
    ]);
  });

  it("排队项已提升为 running 时不会按 pending 取消", async () => {
    create.mockResolvedValue(makeRun("run-promoted", "thread-a", "pending", "2026-08-22T09:00:00Z"));
    get.mockResolvedValue(makeRun("run-promoted", "thread-a", "running", "2026-08-22T09:00:00Z"));
    const { result } = renderHook(() => useThreadRunManager(client, "supervisor"));
    await act(async () => {
      await result.current.enqueue(
        "thread-a",
        { messages: [{ type: "human", content: "待提升" }] },
        "待提升",
      );
    });

    let cancelled = true;
    await act(async () => {
      cancelled = await result.current.cancelPending("thread-a", "run-promoted");
    });

    expect(cancelled).toBe(false);
    expect(cancel).not.toHaveBeenCalled();
    expect(result.current.runtimes["thread-a"]).toMatchObject({
      activeRunId: "run-promoted",
      connection: "detached",
      queuedRuns: [],
    });
  });

  it("旧 run 的延迟回调不会覆盖同一 thread 的新 active run", () => {
    const oldRun = makeRun("run-old", "thread-a", "running", "2026-08-22T09:00:00Z");
    const newRun = makeRun("run-new", "thread-a", "running", "2026-08-22T09:01:00Z");
    const { result } = renderHook(() => useThreadRunManager(client, "supervisor"));

    act(() => {
      result.current.recordCreated(oldRun);
      result.current.recordCreated(newRun);
      result.current.recordFinished(oldRun);
      result.current.recordError(new Error("旧连接关闭"), oldRun);
    });

    expect(result.current.runtimes["thread-a"]).toMatchObject({
      activeRunId: "run-new",
      connection: "connected",
    });
  });

  it("停止 active run 时保留该 thread 的 pending 队列", async () => {
    const active = makeRun("run-active", "thread-a", "running", "2026-08-22T09:00:00Z");
    const pending = makeRun("run-pending", "thread-a", "pending", "2026-08-22T09:01:00Z", "继续执行");
    get.mockResolvedValue(active);
    list.mockImplementation(async (_threadId, options) => (
      options.status === "pending" ? [pending] : []
    ));
    const { result } = renderHook(() => useThreadRunManager(client, "supervisor"));
    act(() => result.current.recordCreated(active));

    await act(async () => {
      await result.current.cancelActive("thread-a");
    });

    expect(cancel).toHaveBeenCalledTimes(1);
    expect(cancel).toHaveBeenCalledWith("thread-a", "run-active", true, "interrupt");
    expect(result.current.runtimes["thread-a"].queuedRuns.map((entry) => entry.id)).toEqual(["run-pending"]);
  });

  it("删除前严格先取消 pending，再取消 running", async () => {
    const active = makeRun("run-active", "thread-a", "running", "2026-08-22T09:00:00Z");
    const pendingA = makeRun("run-pending-a", "thread-a", "pending", "2026-08-22T09:01:00Z");
    const pendingB = makeRun("run-pending-b", "thread-a", "pending", "2026-08-22T09:02:00Z");
    list.mockImplementation(async (_threadId, options) => (
      options.status === "running" ? [active] : [pendingA, pendingB]
    ));
    const { result } = renderHook(() => useThreadRunManager(client, "supervisor"));

    await act(async () => {
      await result.current.cancelAllAndWait("thread-a");
    });

    expect(cancel).toHaveBeenCalledTimes(3);
    expect(cancel.mock.calls.slice(0, 2).map((call) => call[1])).toEqual(["run-pending-a", "run-pending-b"]);
    expect(cancel.mock.calls[2][1]).toBe("run-active");
    await waitFor(() => expect(result.current.runtimes["thread-a"]).toEqual({
      connection: "idle",
      queuedRuns: [],
    }));
  });
});
