import { FormEvent, KeyboardEvent, memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useStream } from "@langchain/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import SessionHistory, { type ConversationThread } from "./SessionHistory";
import TaskTrace, { type AsyncTask, type AsyncTaskStatus } from "./TaskTrace";
import TodoPanel, { type TodoItem } from "./TodoPanel";
import ToolCallCard, { type ToolCard } from "./ToolCallCard";

type RawToolCall = { id?: string; name?: string; args?: unknown };
type Message = {
  id?: string;
  name?: string;
  type: string;
  content: unknown;
  tool_calls?: RawToolCall[];
  tool_call_id?: string;
};

type StreamState = {
  messages: Message[];
  todos?: TodoItem[];
  async_tasks?: Record<string, AsyncTask>;
};

type Row =
  | { kind: "message"; key: string; role: "human" | "ai"; body: string; report: boolean }
  | { kind: "tool"; key: string; card: ToolCard };

type SubmitMode = "enqueue" | "interrupt";
type AuthMode = "login" | "register";
type AsyncTaskStatusResponse = { tasks?: AsyncTask[] };
type AuthUser = {
  id: string;
  username: string;
  is_default: boolean;
};

const AUTH_TOKEN_KEY = "deep-data-auth-token";
const TASK_POLL_INTERVAL_MS = 4_000;
const INITIAL_VISIBLE_ROW_LIMIT = 60;
const ASYNC_TASK_STATUSES = new Set<AsyncTaskStatus>([
  "pending",
  "running",
  "success",
  "error",
  "cancelled",
  "timeout",
  "interrupted",
]);
const DEFAULT_USER: AuthUser = {
  id: "local-user",
  username: "默认账户",
  is_default: true,
};

const EXAMPLES = [
  "抓取 Tavily Python SDK 文档，整理主要接口和适用场景",
  "搜索近一个月数据分析 Agent 的进展，并比较主要方案",
  "分析指定公开网页中的产品、价格和来源信息",
];

function conversationTitle(value: string): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  return normalized.length > 32 ? `${normalized.slice(0, 32)}…` : normalized;
}

function messageText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content
    .map((part) => {
      if (typeof part === "string") return part;
      if (typeof part === "object" && part !== null && "text" in part) {
        return String((part as { text?: unknown }).text ?? "");
      }
      return "";
    })
    .join("");
}

function isReport(body: string): boolean {
  return /(^|\n)#{1,3}\s*(来源|结论|数据质量|分析报告)/m.test(body);
}

function queuedMessageText(values: Partial<StreamState> | null | undefined): string {
  const messages = values?.messages;
  if (!Array.isArray(messages) || messages.length === 0) return "等待处理的消息";
  return messageText(messages[messages.length - 1]?.content).trim() || "等待处理的消息";
}

export function buildRows(messages: Message[]): Row[] {
  const rows: Row[] = [];
  const cards = new Map<string, ToolCard>();

  for (const message of messages) {
    if (message.type === "human" || message.type === "ai") {
      const body = messageText(message.content).trim();
      const hiddenTaskMonitorMessage = message.type === "human" && message.name === "async-task-monitor";
      if (body && !hiddenTaskMonitorMessage) {
        rows.push({
          kind: "message",
          key: message.id ?? `${message.type}-${rows.length}`,
          role: message.type,
          body,
          report: message.type === "ai" && isReport(body),
        });
      }

      for (const call of message.tool_calls ?? []) {
        const callId = call.id ?? `${message.id}-${call.name}-${rows.length}`;
        const card: ToolCard = {
          callId,
          name: call.name ?? "tool",
          args: call.args ?? {},
          result: null,
          status: "pending",
        };
        cards.set(callId, card);
        rows.push({ kind: "tool", key: `tool-${callId}`, card });
      }
      continue;
    }

    if (message.type === "tool" && message.tool_call_id) {
      const card = cards.get(message.tool_call_id);
      if (card) {
        card.result = messageText(message.content);
        card.status = "done";
      }
    }
  }

  return rows;
}

function BrandMark() {
  return (
    <span className="brand-mark" aria-hidden="true">
      <i />
      <i />
      <i />
    </span>
  );
}

type MessageCardProps = {
  messageKey: string;
  role: "human" | "ai";
  body: string;
  report: boolean;
  streaming: boolean;
};

const MessageCard = memo(function MessageCard({
  role,
  body,
  report,
  streaming,
}: MessageCardProps) {
  return (
    <article className={`message message--${role}${report ? " message--report" : ""}`}>
      <header>
        <span>{role === "human" ? "你" : report ? "研究报告" : "Supervisor"}</span>
        <i aria-hidden="true" />
      </header>
      <div className={`markdown-body${streaming ? " markdown-body--streaming" : ""}`}>
        {streaming ? body : <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>}
      </div>
    </article>
  );
}, (previous, next) => (
  previous.messageKey === next.messageKey
  && previous.role === next.role
  && previous.body === next.body
  && previous.report === next.report
  && previous.streaming === next.streaming
));

export default function App() {
  const apiUrl = import.meta.env.VITE_LANGGRAPH_API_URL ?? "http://127.0.0.1:2024";
  const assistantId = import.meta.env.VITE_LANGGRAPH_ASSISTANT_ID ?? "supervisor";
  const [threadId, setThreadId] = useState<string | undefined>(
    () => new URLSearchParams(window.location.search).get("thread") ?? undefined,
  );
  const [input, setInput] = useState("");
  const [authToken, setAuthToken] = useState<string | null>(
    () => window.localStorage.getItem(AUTH_TOKEN_KEY),
  );
  const [authUser, setAuthUser] = useState<AuthUser>(DEFAULT_USER);
  const [authMode, setAuthMode] = useState<AuthMode | null>(null);
  const [authUsername, setAuthUsername] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [authConfirmation, setAuthConfirmation] = useState("");
  const [authError, setAuthError] = useState("");
  const [authSubmitting, setAuthSubmitting] = useState(false);
  const [sessions, setSessions] = useState<ConversationThread[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionsError, setSessionsError] = useState("");
  const [deletingThreadId, setDeletingThreadId] = useState<string>();
  const [polledTasks, setPolledTasks] = useState<Record<string, AsyncTask>>({});
  const [tasksRefreshing, setTasksRefreshing] = useState(false);
  const [taskRefreshError, setTaskRefreshError] = useState("");
  const [visibleRowLimit, setVisibleRowLimit] = useState(INITIAL_VISIBLE_ROW_LIMIT);
  const endRef = useRef<HTMLDivElement>(null);
  const previousLoadingRef = useRef(false);
  const taskPollInFlightRef = useRef(false);
  const autoCollectedTaskRunsRef = useRef<Set<string>>(new Set());
  const authHeaders = useMemo<Record<string, string>>(
    () => {
      const headers: Record<string, string> = {};
      if (authToken) headers.Authorization = `Bearer ${authToken}`;
      return headers;
    },
    [authToken],
  );

  const loadSessions = useCallback(async (signal?: AbortSignal) => {
    setSessionsLoading(true);
    setSessionsError("");
    try {
      const collected: ConversationThread[] = [];
      const limit = 100;
      let offset = 0;
      while (true) {
        const response = await fetch(`${apiUrl}/threads/search`, {
          method: "POST",
          headers: { "Content-Type": "application/json", ...authHeaders },
          body: JSON.stringify({
            metadata: { graph_id: assistantId },
            limit,
            offset,
            sort_by: "updated_at",
            sort_order: "desc",
            select: [
              "thread_id",
              "created_at",
              "updated_at",
              "state_updated_at",
              "metadata",
              "status",
            ],
            extract: { first_message: "values.messages[0].content" },
          }),
          signal,
        });
        if (!response.ok) {
          throw new Error(response.status === 401 ? "登录已失效，请重新登录" : "暂时无法读取会话记录");
        }
        const batch = await response.json() as ConversationThread[];
        if (!Array.isArray(batch)) throw new Error("会话服务返回了无效数据");
        collected.push(...batch);
        if (batch.length < limit) break;
        offset += limit;
      }
      setSessions(collected);
    } catch (error) {
      if (signal?.aborted) return;
      setSessionsError(error instanceof Error ? error.message : "暂时无法读取会话记录");
    } finally {
      if (!signal?.aborted) setSessionsLoading(false);
    }
  }, [apiUrl, assistantId, authHeaders]);

  const stream = useStream<StreamState>({
    apiUrl,
    assistantId,
    threadId,
    // 合并密集 token/state 事件，避免每个事件都触发整页 React 渲染。
    throttle: 60,
    reconnectOnMount: true,
    defaultHeaders: authHeaders,
    onThreadId: (id) => {
      setThreadId(id);
      const url = new URL(window.location.href);
      url.searchParams.set("thread", id);
      window.history.replaceState({}, "", url);
    },
  });

  const rows = useMemo(() => buildRows(stream.messages as Message[]), [stream.messages]);
  const visibleRows = useMemo(
    () => rows.slice(Math.max(0, rows.length - visibleRowLimit)),
    [rows, visibleRowLimit],
  );
  const hiddenRowCount = rows.length - visibleRows.length;
  const lastMessageKey = useMemo(() => {
    for (let index = rows.length - 1; index >= 0; index -= 1) {
      const row = rows[index];
      if (row?.kind === "message") return row.key;
    }
    return undefined;
  }, [rows]);
  const todos = Array.isArray(stream.values?.todos) ? stream.values.todos : [];
  const trackedTasks = useMemo(
    () => Object.values(stream.values?.async_tasks ?? {}).reverse(),
    [stream.values?.async_tasks],
  );
  const tasks = useMemo(
    () => trackedTasks.map((task) => {
      const live = polledTasks[task.task_id];
      if (!live || (live.run_id && task.run_id && live.run_id !== task.run_id)) return task;
      return { ...task, ...live };
    }),
    [polledTasks, trackedTasks],
  );
  const runningTaskCount = tasks.filter(
    (task) => task.status === "running" || task.status === "pending",
  ).length;
  const identitySwitchBlocked = stream.isLoading || stream.queue.size > 0 || runningTaskCount > 0;
  const pollingTaskKey = tasks
    .filter((task) => task.status === "running" || task.status === "pending")
    .map((task) => `${task.task_id}:${task.run_id ?? ""}`)
    .sort()
    .join("|");

  const refreshTaskStatuses = useCallback(async (
    signal?: AbortSignal,
    showLoading = false,
  ) => {
    if (!threadId || taskPollInFlightRef.current) return;
    taskPollInFlightRef.current = true;
    if (showLoading) setTasksRefreshing(true);
    try {
      const response = await fetch(`${apiUrl}/async-tasks/status`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders },
        body: JSON.stringify({ thread_id: threadId }),
        signal,
      });
      if (!response.ok) {
        throw new Error(response.status === 401 ? "登录已失效，请重新登录" : "暂时无法刷新后台任务");
      }
      const payload = await response.json() as AsyncTaskStatusResponse;
      if (!Array.isArray(payload.tasks)) throw new Error("任务状态服务返回了无效数据");

      const checkedAt = new Date().toISOString();
      setPolledTasks((current) => {
        const next = { ...current };
        for (const task of payload.tasks ?? []) {
          if (!task?.task_id || !ASYNC_TASK_STATUSES.has(task.status)) continue;
          next[task.task_id] = { ...task, last_checked_at: checkedAt };
        }
        return next;
      });
      setTaskRefreshError("");
    } catch (error) {
      if (signal?.aborted) return;
      setTaskRefreshError(error instanceof Error ? error.message : "暂时无法刷新后台任务");
    } finally {
      taskPollInFlightRef.current = false;
      if (showLoading && !signal?.aborted) setTasksRefreshing(false);
    }
  }, [apiUrl, authHeaders, threadId]);

  useEffect(() => {
    const controller = new AbortController();
    void loadSessions(controller.signal);
    return () => controller.abort();
  }, [loadSessions]);

  useEffect(() => {
    if (previousLoadingRef.current && !stream.isLoading) void loadSessions();
    previousLoadingRef.current = stream.isLoading;
  }, [loadSessions, stream.isLoading]);

  useEffect(() => {
    if (!authToken) {
      setAuthUser(DEFAULT_USER);
      return;
    }
    const controller = new AbortController();
    void fetch(`${apiUrl}/auth/me`, {
      headers: authHeaders,
      signal: controller.signal,
    })
      .then(async (response) => {
        if (!response.ok) throw new Error(response.status === 401 ? "登录已失效，请重新登录" : "账户服务不可用");
        return response.json() as Promise<{ user: AuthUser }>;
      })
      .then(({ user }) => setAuthUser(user))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        const message = error instanceof Error ? error.message : "账户服务不可用";
        setAuthError(message);
        if (message.includes("登录已失效")) {
          window.localStorage.removeItem(AUTH_TOKEN_KEY);
          setAuthToken(null);
          setAuthUser(DEFAULT_USER);
        }
      });
    return () => controller.abort();
  }, [apiUrl, authHeaders, authToken]);

  useEffect(() => {
    setPolledTasks({});
    setTaskRefreshError("");
    setVisibleRowLimit(INITIAL_VISIBLE_ROW_LIMIT);
    autoCollectedTaskRunsRef.current.clear();
  }, [threadId]);

  useEffect(() => {
    if (!threadId || !pollingTaskKey) return undefined;
    const controller = new AbortController();
    const poll = () => {
      if (!document.hidden) void refreshTaskStatuses(controller.signal);
    };
    const onVisibilityChange = () => {
      if (!document.hidden) poll();
    };

    poll();
    const intervalId = window.setInterval(poll, TASK_POLL_INTERVAL_MS);
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      controller.abort();
      window.clearInterval(intervalId);
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, [pollingTaskKey, refreshTaskStatuses, threadId]);

  useEffect(() => {
    if (stream.isLoading || stream.queue.size > 0) return;
    const trackedById = new Map(trackedTasks.map((task) => [task.task_id, task]));
    const completed = tasks.filter((task) => {
      const tracked = trackedById.get(task.task_id);
      const runKey = `${task.task_id}:${task.run_id ?? ""}`;
      return task.status === "success"
        && tracked?.status !== "success"
        && !autoCollectedTaskRunsRef.current.has(runKey);
    });
    if (completed.length === 0) return;

    const runKeys = completed.map((task) => `${task.task_id}:${task.run_id ?? ""}`);
    runKeys.forEach((key) => autoCollectedTaskRunsRef.current.add(key));
    const taskIds = completed.map((task) => task.task_id).join("、");
    void Promise.resolve(stream.submit(
      {
        messages: [{
          type: "human",
          name: "async-task-monitor",
          content: `后台任务 ${taskIds} 已完成。请调用 check_async_task 读取结果并继续处理，不要重新启动任务。`,
        }],
      },
      { streamResumable: true, onDisconnect: "continue" },
    )).catch(() => {
      runKeys.forEach((key) => autoCollectedTaskRunsRef.current.delete(key));
      setTaskRefreshError("任务已完成，但自动读取结果失败，请点击“读取结果”重试");
    });
  }, [stream.isLoading, stream.queue.size, stream.submit, tasks, trackedTasks]);

  useEffect(() => {
    // jsdom 等非完整浏览器环境可能不提供 scrollIntoView。
    endRef.current?.scrollIntoView?.({
      behavior: stream.isLoading ? "auto" : "smooth",
      block: "end",
    });
  }, [rows.length, stream.isLoading]);

  function submitText(text: string, mode: SubmitMode = "enqueue") {
    const value = text.trim();
    if (!value) return;
    setInput("");
    const multitaskOptions = stream.isLoading
      ? { multitaskStrategy: mode }
      : {};

    void stream.submit(
      { messages: [{ type: "human", content: value }] },
      {
        ...multitaskOptions,
        ...(!threadId && stream.messages.length === 0 ? {
          metadata: {
            kind: "conversation",
            title: conversationTitle(value),
          },
        } : {}),
        streamResumable: true,
        onDisconnect: "continue",
      },
    );
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    submitText(input);
  }

  function onComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submitText(input);
    }
  }

  function checkTask(taskId: string) {
    submitText(
      `请调用 check_async_task 检查任务 ${taskId}。如果远程运行已经结束，请读取结果第一行的业务 status 并继续处理。`,
    );
  }

  function updateTask(taskId: string, message: string) {
    submitText(
      `请调用 update_async_task 更新任务 ${taskId}。补充要求：${message}`,
      "interrupt",
    );
  }

  function cancelTask(taskId: string) {
    const confirmed = window.confirm(
      "确定取消这个后台任务吗？已经完成的请求和文件不会自动删除。",
    );
    if (!confirmed) return;
    submitText(`请调用 cancel_async_task 取消任务 ${taskId}。`, "interrupt");
  }

  function refreshTasks() {
    void refreshTaskStatuses(undefined, true);
  }

  function startNewThread() {
    const activeWorkCount = runningTaskCount + stream.queue.size + Number(stream.isLoading);
    if (
      activeWorkCount > 0
      && !window.confirm(
        "当前还有正在回答、等待处理或后台采集的任务。开始新会话不会取消独立运行的 crawl-worker，且新会话无法直接管理旧任务。仍要继续吗？",
      )
    ) {
      return;
    }
    stream.switchThread(null);
    setThreadId(undefined);
    setInput("");
    window.history.replaceState({}, "", window.location.pathname);
  }

  function selectSession(nextThreadId: string) {
    if (nextThreadId === threadId || stream.isLoading || stream.queue.size > 0) return;
    stream.switchThread(nextThreadId);
    setThreadId(nextThreadId);
    setInput("");
    const url = new URL(window.location.href);
    url.searchParams.set("thread", nextThreadId);
    window.history.replaceState({}, "", url);
  }

  async function deleteSession(targetThreadId: string) {
    if (deletingThreadId || (targetThreadId === threadId && identitySwitchBlocked)) return;
    setDeletingThreadId(targetThreadId);
    setSessionsError("");
    try {
      const response = await fetch(`${apiUrl}/threads/${encodeURIComponent(targetThreadId)}`, {
        method: "DELETE",
        headers: authHeaders,
      });
      if (!response.ok) {
        throw new Error(response.status === 401 ? "登录已失效，请重新登录" : "删除会话失败，请稍后重试");
      }
      setSessions((current) => current.filter((session) => session.thread_id !== targetThreadId));
      if (targetThreadId === threadId) {
        // 删除当前会话后直接进入空白会话，不保留已经删除的 URL。
        stream.switchThread(null);
        setThreadId(undefined);
        setInput("");
        window.history.replaceState({}, "", window.location.pathname);
      }
    } catch (error) {
      setSessionsError(error instanceof Error ? error.message : "删除会话失败，请稍后重试");
    } finally {
      setDeletingThreadId(undefined);
    }
  }

  function resetThreadForIdentityChange() {
    stream.switchThread(null);
    setThreadId(undefined);
    setInput("");
    window.history.replaceState({}, "", window.location.pathname);
  }

  function openAuth(mode: AuthMode) {
    if (identitySwitchBlocked) return;
    setAuthMode(mode);
    setAuthUsername("");
    setAuthPassword("");
    setAuthConfirmation("");
    setAuthError("");
  }

  async function submitAuth(event: FormEvent) {
    event.preventDefault();
    if (!authMode) return;
    setAuthSubmitting(true);
    setAuthError("");
    const payload = authMode === "register"
      ? {
          username: authUsername,
          password: authPassword,
          confirm_password: authConfirmation,
        }
      : { username: authUsername, password: authPassword };
    try {
      const response = await fetch(`${apiUrl}/auth/${authMode}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await response.json() as {
        token?: string;
        user?: AuthUser;
        detail?: string | Array<{ msg?: string }>;
      };
      if (!response.ok || !body.token || !body.user) {
        const detail = Array.isArray(body.detail)
          ? body.detail.map((item) => item.msg).filter(Boolean).join("；")
          : body.detail;
        throw new Error(detail || "账户操作失败，请检查输入后重试");
      }
      window.localStorage.setItem(AUTH_TOKEN_KEY, body.token);
      setAuthToken(body.token);
      setAuthUser(body.user);
      setAuthMode(null);
      resetThreadForIdentityChange();
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : "账户服务不可用");
    } finally {
      setAuthSubmitting(false);
    }
  }

  async function logout() {
    if (!authToken || identitySwitchBlocked) return;
    setAuthError("");
    try {
      const response = await fetch(`${apiUrl}/auth/logout`, {
        method: "POST",
        headers: authHeaders,
      });
      if (!response.ok) throw new Error("注销失败，请稍后重试");
      window.localStorage.removeItem(AUTH_TOKEN_KEY);
      setAuthToken(null);
      setAuthUser(DEFAULT_USER);
      resetThreadForIdentityChange();
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : "账户服务不可用");
    }
  }

  return (
    <div className="workspace-shell">
      <aside className="sidebar">
        <div className="brand">
          <BrandMark />
          <div>
            <strong>深研</strong>
            <span>网页数据工作台</span>
          </div>
        </div>

        <div className="session-card">
          <div className="session-card__status">
            <span className="status-dot" aria-hidden="true" />
            <span>{stream.isLoading ? "Supervisor 正在回答" : "Supervisor 入口就绪"}</span>
          </div>
          <code title={threadId ?? "等待创建"}>{threadId ?? "新会话 · 等待首次消息"}</code>
          <button type="button" onClick={startNewThread}>开始新任务</button>
        </div>

        <SessionHistory
          sessions={sessions}
          currentThreadId={threadId}
          loading={sessionsLoading}
          error={sessionsError}
          switchingDisabled={stream.isLoading || stream.queue.size > 0}
          deletingThreadId={deletingThreadId}
          deleteCurrentDisabled={identitySwitchBlocked}
          onSelect={selectSession}
          onDelete={(targetThreadId) => void deleteSession(targetThreadId)}
          onRefresh={() => void loadSessions()}
        />

        <section className="account-card" aria-label="当前账户">
          <div className="account-card__identity">
            <span aria-hidden="true">{authUser.is_default ? "访" : authUser.username.slice(0, 1).toUpperCase()}</span>
            <div>
              <small>{authUser.is_default ? "共享身份" : "个人空间"}</small>
              <strong>{authUser.username}</strong>
            </div>
          </div>
          {authUser.is_default ? (
            <div className="account-card__actions">
              <button type="button" disabled={identitySwitchBlocked} onClick={() => openAuth("login")}>登录</button>
              <button type="button" disabled={identitySwitchBlocked} onClick={() => openAuth("register")}>注册</button>
            </div>
          ) : (
            <button className="account-card__logout" type="button" disabled={identitySwitchBlocked} onClick={() => void logout()}>
              注销并切回默认账户
            </button>
          )}
          {identitySwitchBlocked ? <p>结束当前运行和后台任务后可切换账户。</p> : null}
          {!authMode && authError ? <p className="account-card__error">{authError}</p> : null}
        </section>

        <div className="sidebar-foot">
          <span>Agent API</span>
          <code>{apiUrl.replace(/^https?:\/\//, "")}</code>
        </div>
      </aside>

      <main className="main-panel">
        <header className="topbar">
          <div>
            <p className="eyebrow">DeepAgents · Tavily</p>
            <h1>从网页线索到可追溯结论</h1>
          </div>
          <div className="topbar__status">
            <div className="runtime-stats" aria-label="运行状态">
              <span>Supervisor：{stream.isLoading ? "回答中" : "空闲"}</span>
              <span>后台任务：{runningTaskCount}</span>
              <span>等待处理：{stream.queue.size}</span>
            </div>
            <i className={stream.isLoading ? "is-active" : ""} aria-hidden="true" />
          </div>
        </header>

        <section className="conversation" aria-label="研究对话">
          {rows.length === 0 ? (
            <div className="empty-state">
              <p className="empty-state__index">研究入口 / 01</p>
              <h2>把一个问题，变成一条证据链。</h2>
              <p className="empty-state__lead">
                描述目标、网址和想分析的数据。Supervisor 会规划任务，后台采集网页，并整理成带来源的 Markdown 报告。
              </p>
              <div className="example-grid">
                {EXAMPLES.map((example) => (
                  <button key={example} type="button" onClick={() => setInput(example)}>
                    <span>示例</span>
                    {example}
                  </button>
                ))}
              </div>
            </div>
          ) : null}

          {hiddenRowCount > 0 ? (
            <button
              type="button"
              className="load-earlier"
              onClick={() => setVisibleRowLimit((current) => current + INITIAL_VISIBLE_ROW_LIMIT)}
            >
              加载更早记录（还有 {hiddenRowCount} 条）
            </button>
          ) : null}

          {visibleRows.map((row) =>
            row.kind === "tool" ? (
              <ToolCallCard key={row.key} card={row.card} />
            ) : (
              <MessageCard
                key={row.key}
                messageKey={row.key}
                role={row.role}
                body={row.body}
                report={row.report}
                streaming={stream.isLoading && row.role === "ai" && row.key === lastMessageKey}
              />
            ),
          )}

          {stream.queue.size > 0 ? (
            <section className="queue-card" aria-labelledby="queue-title">
              <header>
                <div>
                  <p className="eyebrow">服务端队列</p>
                  <h2 id="queue-title">等待处理 · {stream.queue.size}</h2>
                </div>
                <button type="button" onClick={() => void stream.queue.clear()}>
                  清空等待消息
                </button>
              </header>
              <ol>
                {stream.queue.entries.map((entry) => (
                  <li key={entry.id}>
                    <span>{queuedMessageText(entry.values)}</span>
                    <button
                      type="button"
                      onClick={() => void stream.queue.cancel(entry.id)}
                      aria-label={`撤销等待消息：${queuedMessageText(entry.values)}`}
                    >
                      撤销
                    </button>
                  </li>
                ))}
              </ol>
            </section>
          ) : null}

          {stream.isLoading ? (
            <div className="working-card" role="status" aria-live="polite">
              <div className="working-card__signal" aria-hidden="true"><i /><i /><i /></div>
              <div>
                <strong>正在推进研究任务</strong>
                <span>规划、工具调用和结果会实时出现在这里。</span>
              </div>
              <button type="button" onClick={() => void stream.stop()}>停止回答</button>
            </div>
          ) : null}

          {stream.error ? (
            <div className="error-card" role="alert">
              <strong>运行失败</strong>
              <span>{String(stream.error)}</span>
            </div>
          ) : null}
          <div ref={endRef} />
        </section>

        <form className="composer" onSubmit={onSubmit}>
          <div className="composer__field">
            <label htmlFor="research-input">
              {stream.isLoading ? "补充要求或纠正方向" : "描述你的网页数据任务"}
            </label>
            <textarea
              id="research-input"
              rows={3}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={onComposerKeyDown}
              placeholder="例如：抓取某个公开站点的产品页面，比较价格并列出来源……"
            />
            <span>
              {stream.isLoading
                ? "Enter 排队发送 · Shift + Enter 换行"
                : "Enter 发送 · Shift + Enter 换行"}
            </span>
          </div>
          <div className="composer__actions">
            <button
              className="send-button"
              type="submit"
              disabled={!input.trim()}
              aria-label={stream.isLoading ? "排队发送消息" : "发送研究任务"}
            >
              <span>{stream.isLoading ? "排队发送" : "发送任务"}</span>
              <i aria-hidden="true">↗</i>
            </button>
            {stream.isLoading ? (
              <button
                className="interrupt-button"
                type="button"
                disabled={!input.trim()}
                onClick={() => submitText(input, "interrupt")}
              >
                立即纠正
              </button>
            ) : null}
          </div>
        </form>
      </main>

      <aside className="operations-rail" aria-label="研究执行状态">
        <TaskTrace
          tasks={tasks}
          onCheck={checkTask}
          onUpdate={updateTask}
          onCancel={cancelTask}
          onRefresh={refreshTasks}
          refreshing={tasksRefreshing}
          refreshError={taskRefreshError}
        />
        <TodoPanel todos={todos} />
      </aside>

      {authMode ? (
        <div className="auth-overlay" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget && !authSubmitting) setAuthMode(null);
        }}>
          <section className="auth-dialog" role="dialog" aria-modal="true" aria-labelledby="auth-title">
            <header>
              <div>
                <p className="eyebrow">身份空间</p>
                <h2 id="auth-title">{authMode === "login" ? "登录深研" : "创建账户"}</h2>
              </div>
              <button type="button" disabled={authSubmitting} onClick={() => setAuthMode(null)} aria-label="关闭账户窗口">×</button>
            </header>
            <p className="auth-dialog__lead">
              {authMode === "login"
                ? "登录后只加载你的会话、Skill 和研究文件。"
                : "创建独立的数据空间；默认账户内容不会复制进来。"}
            </p>
            <form onSubmit={(event) => void submitAuth(event)}>
              <label>
                用户名
                <input
                  autoFocus
                  autoComplete="username"
                  minLength={3}
                  maxLength={32}
                  pattern={"[A-Za-z0-9][A-Za-z0-9_\\-]{2,31}"}
                  value={authUsername}
                  onChange={(event) => setAuthUsername(event.target.value)}
                  placeholder="3–32 位英文标识符"
                  required
                />
              </label>
              <label>
                密码
                <input
                  type="password"
                  autoComplete={authMode === "login" ? "current-password" : "new-password"}
                  minLength={authMode === "register" ? 8 : 1}
                  maxLength={128}
                  value={authPassword}
                  onChange={(event) => setAuthPassword(event.target.value)}
                  placeholder={authMode === "register" ? "至少 8 个字符" : "输入密码"}
                  required
                />
              </label>
              {authMode === "register" ? (
                <label>
                  确认密码
                  <input
                    type="password"
                    autoComplete="new-password"
                    minLength={8}
                    maxLength={128}
                    value={authConfirmation}
                    onChange={(event) => setAuthConfirmation(event.target.value)}
                    placeholder="再次输入密码"
                    required
                  />
                </label>
              ) : null}
              {authError ? <p className="auth-dialog__error" role="alert">{authError}</p> : null}
              <button className="auth-dialog__submit" type="submit" disabled={authSubmitting}>
                {authSubmitting ? "正在处理…" : authMode === "login" ? "登录并进入个人空间" : "注册并进入个人空间"}
              </button>
            </form>
            <button className="auth-dialog__switch" type="button" disabled={authSubmitting} onClick={() => {
              setAuthMode(authMode === "login" ? "register" : "login");
              setAuthError("");
            }}>
              {authMode === "login" ? "还没有账户？注册" : "已有账户？登录"}
            </button>
          </section>
        </div>
      ) : null}
    </div>
  );
}
