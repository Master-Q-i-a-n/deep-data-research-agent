import { FormEvent, KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import { useStream } from "@langchain/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import TaskTrace, { type AsyncTask } from "./TaskTrace";
import TodoPanel, { type TodoItem } from "./TodoPanel";
import ToolCallCard, { type ToolCard } from "./ToolCallCard";

type RawToolCall = { id?: string; name?: string; args?: unknown };
type Message = {
  id?: string;
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

const EXAMPLES = [
  "抓取 Tavily Python SDK 文档，整理主要接口和适用场景",
  "搜索近一个月数据分析 Agent 的进展，并比较主要方案",
  "分析指定公开网页中的产品、价格和来源信息",
];

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
      if (body) {
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

export default function App() {
  const apiUrl = import.meta.env.VITE_LANGGRAPH_API_URL ?? "http://127.0.0.1:2024";
  const assistantId = import.meta.env.VITE_LANGGRAPH_ASSISTANT_ID ?? "supervisor";
  const [threadId, setThreadId] = useState<string | undefined>(
    () => new URLSearchParams(window.location.search).get("thread") ?? undefined,
  );
  const [input, setInput] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  const stream = useStream<StreamState>({
    apiUrl,
    assistantId,
    threadId,
    reconnectOnMount: true,
    onThreadId: (id) => {
      setThreadId(id);
      const url = new URL(window.location.href);
      url.searchParams.set("thread", id);
      window.history.replaceState({}, "", url);
    },
  });

  const rows = useMemo(() => buildRows(stream.messages as Message[]), [stream.messages]);
  const todos = Array.isArray(stream.values?.todos) ? stream.values.todos : [];
  const tasks = useMemo(
    () => Object.values(stream.values?.async_tasks ?? {}).reverse(),
    [stream.values?.async_tasks],
  );
  const runningTaskCount = tasks.filter(
    (task) => task.status === "running" || task.status === "pending",
  ).length;

  useEffect(() => {
    // jsdom 等非完整浏览器环境可能不提供 scrollIntoView。
    endRef.current?.scrollIntoView?.({ behavior: "smooth", block: "end" });
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
    submitText("请调用 list_async_tasks，刷新当前会话所有异步任务的最新状态。");
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

        <TaskTrace
          tasks={tasks}
          onCheck={checkTask}
          onUpdate={updateTask}
          onCancel={cancelTask}
          onRefresh={refreshTasks}
        />
        <TodoPanel todos={todos} />

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

          {rows.map((row) =>
            row.kind === "tool" ? (
              <ToolCallCard key={row.key} card={row.card} />
            ) : (
              <article
                key={row.key}
                className={`message message--${row.role}${row.report ? " message--report" : ""}`}
              >
                <header>
                  <span>{row.role === "human" ? "你" : row.report ? "研究报告" : "Supervisor"}</span>
                  <i aria-hidden="true" />
                </header>
                <div className="markdown-body">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{row.body}</ReactMarkdown>
                </div>
              </article>
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
    </div>
  );
}
