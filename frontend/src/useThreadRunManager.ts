import { useCallback, useState } from "react";
import type { Client } from "@langchain/langgraph-sdk/client";
import type { Run } from "@langchain/langgraph-sdk";

type RunStatus = "pending" | "running" | "error" | "success" | "timeout" | "interrupted";

export type ThreadConnectionState = "idle" | "connected" | "detached" | "reconnecting" | "error";

type QueuedMessage = {
  type: "human";
  content: string;
  name?: string;
};

export type ManagedQueueEntry = {
  id: string;
  threadId: string;
  createdAt: Date;
  values: { messages: QueuedMessage[] };
  preview: string;
};

export type ThreadRuntimeState = {
  activeRunId?: string;
  connection: ThreadConnectionState;
  queuedRuns: ManagedQueueEntry[];
  error?: string;
};

export type ActiveRuns = {
  running?: Run;
  pending: Run[];
};

type ThreadStatusSnapshot = {
  thread_id: string;
  status?: "idle" | "busy" | "interrupted" | "error";
};

type RunCallbackMeta = {
  run_id: string;
  thread_id: string;
};

const RUN_PAGE_SIZE = 100;

function emptyRuntime(): ThreadRuntimeState {
  return { connection: "idle", queuedRuns: [] };
}

function queuePreview(run: Run): string {
  const ui = run.metadata?.deep_data_ui;
  if (typeof ui !== "object" || ui === null) return "排队任务";
  const preview = (ui as { preview?: unknown }).preview;
  return typeof preview === "string" && preview.trim() ? preview.trim() : "排队任务";
}

function restoredQueueEntry(run: Run, existing?: ManagedQueueEntry): ManagedQueueEntry {
  if (existing) return existing;
  const preview = queuePreview(run);
  return {
    id: run.run_id,
    threadId: run.thread_id,
    createdAt: new Date(run.created_at),
    values: { messages: [{ type: "human", content: preview }] },
    preview,
  };
}

async function listRunsByStatus(
  client: Client,
  threadId: string,
  status: RunStatus,
  signal?: AbortSignal,
): Promise<Run[]> {
  const runs: Run[] = [];
  let offset = 0;
  while (true) {
    const batch = await client.runs.list(threadId, {
      status,
      limit: RUN_PAGE_SIZE,
      offset,
      select: ["run_id", "thread_id", "assistant_id", "created_at", "updated_at", "status", "metadata", "multitask_strategy"],
      signal,
    });
    runs.push(...batch);
    if (batch.length < RUN_PAGE_SIZE) return runs;
    offset += RUN_PAGE_SIZE;
  }
}

export function useThreadRunManager(client: Client, assistantId: string) {
  const [runtimes, setRuntimes] = useState<Record<string, ThreadRuntimeState>>({});

  const updateRuntime = useCallback((
    threadId: string,
    update: (current: ThreadRuntimeState) => ThreadRuntimeState,
  ) => {
    setRuntimes((current) => ({
      ...current,
      [threadId]: update(current[threadId] ?? emptyRuntime()),
    }));
  }, []);

  const reconcile = useCallback(async (threadId: string, signal?: AbortSignal): Promise<ActiveRuns> => {
    const [runningRuns, pendingRuns] = await Promise.all([
      listRunsByStatus(client, threadId, "running", signal),
      listRunsByStatus(client, threadId, "pending", signal),
    ]);
    const running = [...runningRuns].sort((left, right) => left.created_at.localeCompare(right.created_at))[0];
    const pending = [...pendingRuns].sort((left, right) => left.created_at.localeCompare(right.created_at));

    updateRuntime(threadId, (current) => {
      const existing = new Map(current.queuedRuns.map((entry) => [entry.id, entry]));
      const queuedRuns = pending
        .filter((run) => run.run_id !== running?.run_id)
        .map((run) => restoredQueueEntry(run, existing.get(run.run_id)));
      const sameConnectedRun = running?.run_id === current.activeRunId
        && (current.connection === "connected" || current.connection === "reconnecting");
      return {
        activeRunId: running?.run_id,
        connection: running
          ? sameConnectedRun ? current.connection : "detached"
          : queuedRuns.length > 0 ? "detached" : "idle",
        queuedRuns,
      };
    });
    return { running, pending };
  }, [client, updateRuntime]);

  const enqueue = useCallback(async (
    threadId: string,
    values: { messages: QueuedMessage[] },
    preview: string,
  ): Promise<ManagedQueueEntry> => {
    const submissionId = window.crypto.randomUUID();
    const normalizedPreview = preview.replace(/\s+/g, " ").trim().slice(0, 160);
    const run = await client.runs.create(threadId, assistantId, {
      input: values,
      metadata: {
        deep_data_ui: {
          submission_id: submissionId,
          preview: normalizedPreview,
        },
      },
      multitaskStrategy: "enqueue",
      streamResumable: true,
      streamSubgraphs: true,
    });
    const entry: ManagedQueueEntry = {
      id: run.run_id,
      threadId,
      createdAt: new Date(run.created_at),
      values,
      preview: normalizedPreview,
    };
    updateRuntime(threadId, (current) => {
      if (run.status === "running") {
        return {
          ...current,
          activeRunId: run.run_id,
          connection: "detached",
          queuedRuns: current.queuedRuns.filter((item) => item.id !== run.run_id),
        };
      }
      return run.status === "pending"
        ? { ...current, queuedRuns: [...current.queuedRuns, entry] }
        : current;
    });
    return entry;
  }, [assistantId, client, updateRuntime]);

  const markDetached = useCallback((threadId: string) => {
    updateRuntime(threadId, (current) => ({
      ...current,
      connection: current.activeRunId || current.queuedRuns.length > 0 ? "detached" : "idle",
    }));
  }, [updateRuntime]);

  const markConnecting = useCallback((threadId: string, runId: string) => {
    updateRuntime(threadId, (current) => ({
      ...current,
      activeRunId: runId,
      connection: "reconnecting",
      error: undefined,
      queuedRuns: current.queuedRuns.filter((entry) => entry.id !== runId),
    }));
  }, [updateRuntime]);

  const markConnected = useCallback((threadId: string, runId: string) => {
    updateRuntime(threadId, (current) => ({
      ...current,
      activeRunId: runId,
      connection: "connected",
      error: undefined,
    }));
  }, [updateRuntime]);

  const recordCreated = useCallback((run: RunCallbackMeta) => {
    updateRuntime(run.thread_id, (current) => ({
      ...current,
      activeRunId: run.run_id,
      connection: "connected",
      error: undefined,
      queuedRuns: current.queuedRuns.filter((entry) => entry.id !== run.run_id),
    }));
  }, [updateRuntime]);

  const recordFinished = useCallback((run?: RunCallbackMeta) => {
    if (!run) return;
    updateRuntime(run.thread_id, (current) => {
      // 延迟到达的旧 run 回调不能覆盖同一 thread 已经开始的新 run。
      if (current.activeRunId && current.activeRunId !== run.run_id) return current;
      return {
        ...current,
        activeRunId: undefined,
        connection: current.queuedRuns.length > 0 ? "detached" : "idle",
        error: undefined,
      };
    });
  }, [updateRuntime]);

  const recordError = useCallback((error: unknown, run?: RunCallbackMeta) => {
    if (!run) return;
    updateRuntime(run.thread_id, (current) => (
      current.activeRunId && current.activeRunId !== run.run_id
        ? current
        : {
            ...current,
            connection: "error",
            error: error instanceof Error ? error.message : String(error),
          }
    ));
  }, [updateRuntime]);

  const cancelPending = useCallback(async (threadId: string, runId: string): Promise<boolean> => {
    const run = await client.runs.get(threadId, runId);
    if (run.status === "running") {
      updateRuntime(threadId, (current) => ({
        ...current,
        activeRunId: runId,
        connection: "detached",
        queuedRuns: current.queuedRuns.filter((entry) => entry.id !== runId),
      }));
      return false;
    }
    if (run.status === "pending") await client.runs.cancel(threadId, runId, true, "interrupt");
    updateRuntime(threadId, (current) => ({
      ...current,
      queuedRuns: current.queuedRuns.filter((entry) => entry.id !== runId),
    }));
    return run.status === "pending";
  }, [client, updateRuntime]);

  const clearPending = useCallback(async (threadId: string): Promise<void> => {
    const { pending } = await reconcile(threadId);
    // 每项取消前重新读取状态，避免把刚被 FIFO 提升的 running run 当作排队项取消。
    await Promise.all(pending.map((run) => cancelPending(threadId, run.run_id)));
    await reconcile(threadId);
  }, [cancelPending, reconcile]);

  const cancelActive = useCallback(async (threadId: string): Promise<ActiveRuns> => {
    const knownRunId = runtimes[threadId]?.activeRunId;
    let run = knownRunId ? await client.runs.get(threadId, knownRunId) : undefined;
    if (!run || (run.status !== "running" && run.status !== "pending")) {
      const active = await reconcile(threadId);
      run = active.running ?? active.pending[0];
    }
    if (run && (run.status === "running" || run.status === "pending")) {
      await client.runs.cancel(threadId, run.run_id, true, "interrupt");
      updateRuntime(threadId, (current) => ({
        ...current,
        activeRunId: undefined,
        connection: "idle",
        queuedRuns: current.queuedRuns.filter((entry) => entry.id !== run.run_id),
      }));
    }
    return reconcile(threadId);
  }, [client, reconcile, runtimes, updateRuntime]);

  const cancelAllAndWait = useCallback(async (threadId: string): Promise<void> => {
    const active = await reconcile(threadId);
    // 先取消 pending，防止取消 active 时下一项被提升为 running。
    await Promise.all(active.pending.map((run) => client.runs.cancel(threadId, run.run_id, true, "interrupt")));
    const running = active.running
      ? [active.running]
      : await listRunsByStatus(client, threadId, "running");
    await Promise.all(running.map((run) => client.runs.cancel(threadId, run.run_id, true, "interrupt")));
    updateRuntime(threadId, () => emptyRuntime());
  }, [client, reconcile, updateRuntime]);

  const syncThreadStatuses = useCallback((threads: ThreadStatusSnapshot[]) => {
    const statuses = new Map(threads.map((thread) => [thread.thread_id, thread.status]));
    setRuntimes((current) => {
      let changed = false;
      const next = { ...current };
      for (const [threadId, runtime] of Object.entries(current)) {
        const status = statuses.get(threadId);
        if (status === "idle" && (runtime.activeRunId || runtime.queuedRuns.length > 0 || runtime.connection !== "idle")) {
          next[threadId] = emptyRuntime();
          changed = true;
        } else if (status === "error" && runtime.connection !== "error") {
          next[threadId] = { ...runtime, activeRunId: undefined, connection: "error" };
          changed = true;
        } else if (status === "interrupted" && runtime.activeRunId) {
          next[threadId] = { ...runtime, activeRunId: undefined, connection: "idle" };
          changed = true;
        }
      }
      return changed ? next : current;
    });
  }, []);

  const removeThread = useCallback((threadId: string) => {
    setRuntimes((current) => {
      if (!(threadId in current)) return current;
      const next = { ...current };
      delete next[threadId];
      return next;
    });
  }, []);

  const reset = useCallback(() => setRuntimes({}), []);

  return {
    runtimes,
    enqueue,
    reconcile,
    markDetached,
    markConnecting,
    markConnected,
    recordCreated,
    recordFinished,
    recordError,
    cancelActive,
    cancelPending,
    clearPending,
    cancelAllAndWait,
    syncThreadStatuses,
    removeThread,
    reset,
  };
}
