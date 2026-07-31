export type ConversationThread = {
  thread_id: string;
  created_at?: string;
  updated_at?: string;
  state_updated_at?: string;
  status?: "idle" | "busy" | "interrupted" | "error";
  metadata?: Record<string, unknown>;
  extracted?: { first_message?: unknown };
};

function sessionTitle(thread: ConversationThread): string {
  const title = thread.metadata?.title;
  const firstMessage = thread.extracted?.first_message;
  const value = typeof title === "string" && title.trim()
    ? title.trim()
    : typeof firstMessage === "string"
      ? firstMessage.replace(/\s+/g, " ").trim()
      : "";
  if (!value) return "未命名研究";
  return value.length > 32 ? `${value.slice(0, 32)}…` : value;
}

function formatSessionTime(thread: ConversationThread): string {
  const value = thread.state_updated_at ?? thread.updated_at ?? thread.created_at;
  if (!value) return "时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).format(date);
}

type SessionHistoryProps = {
  sessions: ConversationThread[];
  currentThreadId?: string;
  loading: boolean;
  error: string;
  switchingDisabled: boolean;
  onSelect: (threadId: string) => void;
  onRefresh: () => void;
};

export default function SessionHistory({
  sessions,
  currentThreadId,
  loading,
  error,
  switchingDisabled,
  onSelect,
  onRefresh,
}: SessionHistoryProps) {
  return (
    <section className="session-history" aria-labelledby="session-history-title">
      <div className="session-history__heading">
        <div>
          <p className="eyebrow">研究档案</p>
          <h2 id="session-history-title">会话记录</h2>
        </div>
        <div>
          <span>{sessions.length}</span>
          <button type="button" onClick={onRefresh} disabled={loading} aria-label="刷新会话记录">
            {loading ? "…" : "↻"}
          </button>
        </div>
      </div>

      {error ? <p className="session-history__error">{error}</p> : null}
      {!loading && sessions.length === 0 ? (
        <p className="session-history__empty">发送第一条研究任务后，会话会保存在这里。</p>
      ) : null}

      <ol className="session-history__list">
        {sessions.map((session) => {
          const active = session.thread_id === currentThreadId;
          return (
            <li key={session.thread_id}>
              <button
                type="button"
                className={active ? "is-active" : ""}
                disabled={switchingDisabled && !active}
                onClick={() => onSelect(session.thread_id)}
                aria-current={active ? "page" : undefined}
              >
                <span className={`session-history__status session-history__status--${session.status ?? "idle"}`} aria-hidden="true" />
                <span className="session-history__content">
                  <strong>{sessionTitle(session)}</strong>
                  <small>{formatSessionTime(session)}</small>
                </span>
              </button>
            </li>
          );
        })}
      </ol>
    </section>
  );
}
