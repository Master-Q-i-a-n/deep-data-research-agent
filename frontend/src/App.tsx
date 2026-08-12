import { ChangeEvent, FormEvent, ImgHTMLAttributes, KeyboardEvent, memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { HumanMessage } from "@langchain/core/messages";
import { useStream } from "@langchain/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import SessionHistory, { type ConversationThread } from "./SessionHistory";
import SubagentPlanPanel from "./SubagentPlanPanel";
import TaskTrace, { type AsyncTask, type AsyncTaskStatus } from "./TaskTrace";
import TodoPanel, { type TodoItem } from "./TodoPanel";
import ToolCallCard, { type ToolCard } from "./ToolCallCard";
import type { SubagentTraceStream } from "./SubagentTrace";

type RawToolCall = { id?: string; name?: string; args?: unknown };
type Message = {
  id?: string;
  name?: string;
  type: string;
  content: unknown;
  tool_calls?: RawToolCall[];
  tool_call_id?: string;
  artifact?: unknown;
};

type StreamState = {
  messages: Message[];
  todos?: TodoItem[];
  async_tasks?: Record<string, AsyncTask>;
};

type Row =
  | { kind: "message"; key: string; role: "human" | "ai"; body: string; report: boolean }
  | { kind: "tool"; key: string; card: ToolCard };

type CurrentTurn = {
  userKey: string;
  userBody: string;
  assistantBody: string;
  assistantReport: boolean;
};

type SubmitMode = "enqueue" | "interrupt";
type AuthMode = "login" | "register";
type AsyncTaskStatusResponse = { tasks?: AsyncTask[] };
type DownloadableArtifact = {
  path: string;
  filename: string;
  size: number;
  mime_type?: string;
};
type ArtifactListResponse = { artifacts?: DownloadableArtifact[] };
type UploadedTableFile = {
  key: string;
  name: string;
  path?: string;
  size: number;
  media_type?: string;
  status: "uploading" | "ready" | "deleting" | "error";
  error?: string;
  source?: File;
};
type FileListResponse = {
  files?: Array<Pick<UploadedTableFile, "name" | "path" | "size" | "media_type">>;
};
type HITLActionRequest = {
  name: string;
  args: Record<string, unknown>;
  description?: string;
};
type HITLReviewConfig = {
  action_name: string;
  allowed_decisions: Array<"approve" | "reject" | "respond" | "edit">;
};
type HITLRequest = {
  action_requests: HITLActionRequest[];
  review_configs: HITLReviewConfig[];
};
type HITLDecision =
  | { type: "approve" }
  | { type: "reject"; message?: string }
  | { type: "respond"; message: string };
type AuthUser = {
  id: string;
  username: string;
  is_default: boolean;
};

const AUTH_TOKEN_KEY = "deep-data-auth-token";
const TASK_POLL_INTERVAL_MS = 4_000;
const INITIAL_VISIBLE_ROW_LIMIT = 60;
const EMPTY_ROWS: Row[] = [];
const MAX_UPLOAD_FILES = 5;
const MAX_UPLOAD_FILE_BYTES = 50 * 1024 * 1024;
const MAX_UPLOAD_TOTAL_BYTES = 100 * 1024 * 1024;
const TABLE_FILE_PATTERN = /\.(csv|tsv|xlsx)$/i;
const ASYNC_TASK_STATUSES = new Set<AsyncTaskStatus>([
  "pending",
  "running",
  "success",
  "error",
  "cancelled",
  "timeout",
  "interrupted",
]);
const FAILED_ASYNC_TASK_STATUSES = new Set<AsyncTaskStatus>([
  "error",
  "timeout",
  "interrupted",
]);
const TASK_FAILURE_LABEL: Partial<Record<AsyncTaskStatus, string>> = {
  error: "执行失败",
  timeout: "执行超时",
  interrupted: "执行中断",
};
const DEFAULT_USER: AuthUser = {
  id: "local-user",
  username: "默认账户",
  is_default: true,
};

const EXAMPLES = [
  "抓取 Tavily Python SDK 文档，整理主要接口和适用场景",
  "搜索近一个月数据分析 Agent 的进展，并比较主要方案",
  "上传 Excel 或 CSV，分析趋势、异常值并生成图表",
];

function conversationTitle(value: string): string {
  const normalized = value.replace(/\s+/g, " ").trim();
  return normalized.length > 32 ? `${normalized.slice(0, 32)}…` : normalized;
}

function viewportAtBottom(): boolean {
  const scrollRoot = document.scrollingElement ?? document.documentElement;
  const scrollTop = Math.max(scrollRoot.scrollTop, window.scrollY);
  return scrollRoot.scrollHeight - (scrollTop + window.innerHeight) <= 72;
}

function scrollViewportToBottom(): void {
  const scrollRoot = document.scrollingElement ?? document.documentElement;
  // 滚到文档真实末端；对话区自身带有底部留白，滚动末端占位节点会提前停下。
  window.scrollTo({ top: scrollRoot.scrollHeight, behavior: "auto" });
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

function hitlRequest(value: unknown): HITLRequest | null {
  if (typeof value !== "object" || value === null) return null;
  const raw = value as Partial<HITLRequest>;
  if (!Array.isArray(raw.action_requests) || !Array.isArray(raw.review_configs)) return null;
  if (raw.action_requests.length === 0 || raw.action_requests.length !== raw.review_configs.length) {
    return null;
  }
  return {
    action_requests: raw.action_requests,
    review_configs: raw.review_configs,
  };
}

function fileDownloadArtifact(value: unknown): DownloadableArtifact | null {
  if (typeof value !== "object" || value === null) return null;
  const raw = value as Partial<DownloadableArtifact> & { type?: unknown };
  if (
    raw.type !== "file_download"
    || typeof raw.path !== "string"
    || typeof raw.filename !== "string"
    || typeof raw.size !== "number"
  ) {
    return null;
  }
  return { path: raw.path, filename: raw.filename, size: raw.size };
}

function formatFileSize(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

function localImagePaths(source: string): string[] {
  const normalized = source.trim().replace(/\\/g, "/").split(/[?#]/, 1)[0];
  if (!normalized || /^(https?:|data:|blob:)/i.test(normalized)) return [];
  const parts = normalized.split("/").filter((part) => part && part !== ".");
  if (parts.includes("..")) return [];
  if (normalized.startsWith("/workspace/")) return [normalized];
  if (normalized.startsWith("workspace/")) return [`/${normalized}`];
  const relative = parts.join("/");
  if (!relative) return [];
  if (relative.startsWith("output/")) return [`/workspace/${relative}`];
  // 新报告把图片放在 output/charts；第二个路径兼容旧报告的 workspace/charts。
  return [`/workspace/output/${relative}`, `/workspace/${relative}`];
}

type AuthenticatedMarkdownImageProps = ImgHTMLAttributes<HTMLImageElement> & {
  apiUrl: string;
  authHeaders: Record<string, string>;
  threadId?: string;
};

function AuthenticatedMarkdownImage({
  apiUrl,
  authHeaders,
  threadId,
  src,
  alt,
  ...props
}: AuthenticatedMarkdownImageProps) {
  const source = typeof src === "string" ? src : "";
  const directSource = /^(https?:|data:|blob:)/i.test(source) ? source : "";
  const [resolvedSource, setResolvedSource] = useState(directSource);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (directSource) {
      setResolvedSource(directSource);
      setFailed(false);
      return undefined;
    }
    const candidates = localImagePaths(source);
    if (!threadId || candidates.length === 0) {
      setResolvedSource("");
      setFailed(true);
      return undefined;
    }

    const controller = new AbortController();
    let objectUrl = "";
    setResolvedSource("");
    setFailed(false);
    void (async () => {
      for (const path of candidates) {
        const query = new URLSearchParams({ path });
        const response = await fetch(
          `${apiUrl}/artifacts/${encodeURIComponent(threadId)}/download?${query.toString()}`,
          { headers: authHeaders, signal: controller.signal },
        );
        if (response.ok) {
          objectUrl = window.URL.createObjectURL(await response.blob());
          setResolvedSource(objectUrl);
          return;
        }
        if (response.status === 401) break;
      }
      if (!controller.signal.aborted) setFailed(true);
    })().catch(() => {
      if (!controller.signal.aborted) setFailed(true);
    });

    return () => {
      controller.abort();
      if (objectUrl) window.URL.revokeObjectURL(objectUrl);
    };
  }, [apiUrl, authHeaders, directSource, source, threadId]);

  if (failed) {
    return <span className="markdown-image-error" role="img" aria-label={alt ?? "图片加载失败"}>图片无法加载：{alt || source}</span>;
  }
  if (!resolvedSource) return <span className="markdown-image-loading">图片加载中…</span>;
  return <img {...props} src={resolvedSource} alt={alt ?? ""} />;
}

function taskRunKey(task: AsyncTask): string {
  return `${task.task_id}:${task.run_id ?? ""}`;
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

function currentTurn(messages: Message[]): CurrentTurn | null {
  let userIndex = -1;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message?.type === "human" && message.name !== "async-task-monitor") {
      userIndex = index;
      break;
    }
  }
  if (userIndex < 0) return null;

  const userMessage = messages[userIndex];
  const userBody = messageText(userMessage.content).trim();
  if (!userBody) return null;

  // 一个可见用户请求可能被内部监控消息续跑；把该请求之后的 Supervisor
  // 文本合并到同一个输出框，但不把工具和子智能体消息带入轻量视图。
  const assistantBody = messages
    .slice(userIndex + 1)
    .filter((message) => message.type === "ai")
    .map((message) => messageText(message.content).trim())
    .filter(Boolean)
    .join("\n\n");

  return {
    userKey: userMessage.id ?? `human-${userIndex}`,
    userBody,
    assistantBody,
    assistantReport: isReport(assistantBody),
  };
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
  apiUrl: string;
  authHeaders: Record<string, string>;
  threadId?: string;
};

const MessageCard = memo(function MessageCard({
  role,
  body,
  report,
  streaming,
  apiUrl,
  authHeaders,
  threadId,
}: MessageCardProps) {
  return (
    <article className={`message message--${role}${report ? " message--report" : ""}`}>
      <header>
        <span>{role === "human" ? "你" : report ? "研究报告" : "Supervisor"}</span>
        <i aria-hidden="true" />
      </header>
      <div className={`markdown-body${streaming ? " markdown-body--streaming" : ""}`}>
        {streaming ? body : (
          <ReactMarkdown
            remarkPlugins={[remarkGfm]}
            components={{
              img: (props) => (
                <AuthenticatedMarkdownImage
                  {...props}
                  apiUrl={apiUrl}
                  authHeaders={authHeaders}
                  threadId={threadId}
                />
              ),
            }}
          >
            {body}
          </ReactMarkdown>
        )}
      </div>
    </article>
  );
}, (previous, next) => (
  previous.messageKey === next.messageKey
  && previous.role === next.role
  && previous.body === next.body
  && previous.report === next.report
  && previous.streaming === next.streaming
  && previous.apiUrl === next.apiUrl
  && previous.authHeaders === next.authHeaders
  && previous.threadId === next.threadId
));

type CompactTurnViewProps = CurrentTurn & {
  streaming: boolean;
  apiUrl: string;
  authHeaders: Record<string, string>;
  threadId?: string;
  onStop: () => void;
};

const CompactTurnView = memo(function CompactTurnView({
  userKey,
  userBody,
  assistantBody,
  assistantReport,
  streaming,
  apiUrl,
  authHeaders,
  threadId,
  onStop,
}: CompactTurnViewProps) {
  const placeholder = streaming ? "正在规划或调用工具…" : "本轮尚未产生模型输出。";
  return (
    <div className="compact-turn">
      <MessageCard
        messageKey={userKey}
        role="human"
        body={userBody}
        report={false}
        streaming={false}
        apiUrl={apiUrl}
        authHeaders={authHeaders}
        threadId={threadId}
      />
      <article className={`message message--ai compact-turn__output${assistantReport ? " message--report" : ""}`}>
        <header>
          <span>Supervisor</span>
          <i aria-hidden="true" />
          <span className={`compact-turn__status${streaming ? " is-running" : ""}`}>
            {streaming ? "生成中" : "已完成"}
          </span>
          {streaming ? (
            <button type="button" onClick={onStop}>停止回答</button>
          ) : null}
        </header>
        <div
          className={`markdown-body${streaming ? " markdown-body--streaming" : ""}${assistantBody ? "" : " compact-turn__placeholder"}`}
          aria-live="polite"
        >
          {assistantBody ? (
            streaming ? assistantBody : (
              <ReactMarkdown
                remarkPlugins={[remarkGfm]}
                components={{
                  img: (props) => (
                    <AuthenticatedMarkdownImage
                      {...props}
                      apiUrl={apiUrl}
                      authHeaders={authHeaders}
                      threadId={threadId}
                    />
                  ),
                }}
              >
                {assistantBody}
              </ReactMarkdown>
            )
          ) : placeholder}
        </div>
      </article>
    </div>
  );
});

function InterruptCard({
  request,
  submitting,
  onResume,
}: {
  request: HITLRequest;
  submitting: boolean;
  onResume: (decisions: HITLDecision[]) => void;
}) {
  const [responses, setResponses] = useState<Record<number, string>>({});
  const [choices, setChoices] = useState<Record<number, "approve" | "reject">>({});

  const ready = request.action_requests.every((_action, index) => {
    const allowed = request.review_configs[index]?.allowed_decisions ?? [];
    if (allowed.includes("respond")) return Boolean(responses[index]?.trim());
    return Boolean(choices[index]);
  });

  function submitDecisions() {
    if (!ready) return;
    const decisions: HITLDecision[] = request.action_requests.map((_action, index) => {
      const allowed = request.review_configs[index]?.allowed_decisions ?? [];
      if (allowed.includes("respond")) {
        return { type: "respond", message: (responses[index] ?? "").trim() };
      }
      const choice = choices[index];
      return choice === "approve"
        ? { type: "approve" }
        : { type: "reject", message: "用户拒绝执行该操作。" };
    });
    onResume(decisions);
  }

  return (
    <section className="interrupt-card" aria-labelledby="interrupt-title">
      <header>
        <div>
          <p className="eyebrow">需要你的决定</p>
          <h2 id="interrupt-title">任务已安全暂停</h2>
        </div>
      </header>
      {request.action_requests.map((action, index) => {
        const allowed = request.review_configs[index]?.allowed_decisions ?? [];
        const isQuestion = action.name === "ask_user" && allowed.includes("respond");
        const question = typeof action.args.question === "string"
          ? action.args.question
          : action.description ?? "请补充完成任务所需的信息";
        const missing = Array.isArray(action.args.missing_fields)
          ? action.args.missing_fields.map(String).join("、")
          : "";
        const known = typeof action.args.known_information === "string"
          ? action.args.known_information
          : "";
        const filePath = typeof action.args.file_path === "string"
          ? action.args.file_path
          : "/workspace/output/final_report.pdf";

        return (
          <div className="interrupt-card__request" key={`${action.name}-${index}`}>
            <strong>{isQuestion ? question : "是否允许下载此文件？"}</strong>
            {isQuestion ? (
              <>
                {missing ? <span>待补充：{missing}</span> : null}
                {known ? <span>已知信息：{known}</span> : null}
                <textarea
                  rows={3}
                  value={responses[index] ?? ""}
                  onChange={(event) => setResponses((current) => ({
                    ...current,
                    [index]: event.target.value,
                  }))}
                  placeholder="请输入补充信息"
                  disabled={submitting}
                />
              </>
            ) : (
              <>
                <code>{filePath}</code>
                <div className="interrupt-card__choices">
                  {allowed.includes("approve") ? (
                    <button
                      type="button"
                      className={choices[index] === "approve" ? "is-selected" : ""}
                      onClick={() => setChoices((current) => ({ ...current, [index]: "approve" }))}
                      disabled={submitting}
                    >
                      批准下载
                    </button>
                  ) : null}
                  {allowed.includes("reject") ? (
                    <button
                      type="button"
                      className={choices[index] === "reject" ? "is-rejected" : ""}
                      onClick={() => setChoices((current) => ({ ...current, [index]: "reject" }))}
                      disabled={submitting}
                    >
                      拒绝
                    </button>
                  ) : null}
                </div>
              </>
            )}
          </div>
        );
      })}
      <button
        type="button"
        className="interrupt-card__submit"
        disabled={!ready || submitting}
        onClick={submitDecisions}
      >
        {submitting ? "正在恢复任务…" : "提交并继续"}
      </button>
    </section>
  );
}

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
  const [dismissedTaskFailures, setDismissedTaskFailures] = useState<Set<string>>(
    () => new Set(),
  );
  const [artifacts, setArtifacts] = useState<DownloadableArtifact[]>([]);
  const [artifactsLoading, setArtifactsLoading] = useState(false);
  const [artifactError, setArtifactError] = useState("");
  const [downloadingPath, setDownloadingPath] = useState<string>();
  const [uploadedFiles, setUploadedFiles] = useState<UploadedTableFile[]>([]);
  const [filesLoading, setFilesLoading] = useState(false);
  const [filesUploading, setFilesUploading] = useState(false);
  const [fileError, setFileError] = useState("");
  const [interruptSubmitting, setInterruptSubmitting] = useState(false);
  const [interruptError, setInterruptError] = useState("");
  const [showDetails, setShowDetails] = useState(false);
  const [visibleRowLimit, setVisibleRowLimit] = useState(INITIAL_VISIBLE_ROW_LIMIT);
  const [showJumpToBottom, setShowJumpToBottom] = useState(false);
  const conversationRef = useRef<HTMLElement>(null);
  const autoFollowRef = useRef(true);
  const previousLoadingRef = useRef(false);
  const taskPollInFlightRef = useRef(false);
  const autoCollectedTaskRunsRef = useRef<Set<string>>(new Set());
  const approvedSemanticDownloadRef = useRef(false);
  const semanticDownloadBaselineRef = useRef<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const mainSnapshotRef = useRef<{
    threadId?: string;
    messages: Message[];
    values: StreamState;
  }>();
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

  const loadArtifacts = useCallback(async (signal?: AbortSignal) => {
    if (!threadId) {
      setArtifacts([]);
      return;
    }
    setArtifactsLoading(true);
    try {
      const response = await fetch(`${apiUrl}/artifacts/${encodeURIComponent(threadId)}`, {
        headers: authHeaders,
        signal,
      });
      if (!response.ok) {
        throw new Error(response.status === 401 ? "登录已失效，请重新登录" : "暂时无法读取研究产物");
      }
      const payload = await response.json() as ArtifactListResponse;
      setArtifacts(Array.isArray(payload.artifacts) ? payload.artifacts : []);
      setArtifactError("");
    } catch (error) {
      if (signal?.aborted) return;
      setArtifactError(error instanceof Error ? error.message : "暂时无法读取研究产物");
    } finally {
      if (!signal?.aborted) setArtifactsLoading(false);
    }
  }, [apiUrl, authHeaders, threadId]);

  const loadUploadedFiles = useCallback(async (signal?: AbortSignal) => {
    if (!threadId) {
      setUploadedFiles([]);
      return;
    }
    setFilesLoading(true);
    try {
      const response = await fetch(`${apiUrl}/files/${encodeURIComponent(threadId)}`, {
        headers: authHeaders,
        signal,
      });
      if (!response.ok) {
        throw new Error(response.status === 401 ? "登录已失效，请重新登录" : "暂时无法读取已上传文件");
      }
      const payload = await response.json() as FileListResponse;
      const files = Array.isArray(payload.files) ? payload.files : [];
      const restored = files.map((file) => ({
        ...file,
        key: file.path ?? `${file.name}-${file.size}`,
        status: "ready" as const,
      }));
      const restoredNames = new Set(restored.map((file) => file.name.toLocaleLowerCase()));
      setUploadedFiles((current) => [
        ...restored,
        ...current.filter(
          (file) => file.status === "error" && !restoredNames.has(file.name.toLocaleLowerCase()),
        ),
      ]);
      setFileError("");
    } catch (error) {
      if (signal?.aborted) return;
      setFileError(error instanceof Error ? error.message : "暂时无法读取已上传文件");
    } finally {
      if (!signal?.aborted) setFilesLoading(false);
    }
  }, [apiUrl, authHeaders, threadId]);

  // 当前前端只持有远程图的状态类型，无法把 Python DeepAgent 类型直接传给
  // useStream；使用宽化后的选项仍可启用 SDK 内置的子智能体跟踪能力。
  const streamOptions = {
    apiUrl,
    assistantId,
    threadId,
    // 合并密集 token/state 事件，避免每个事件都触发整页 React 渲染。
    throttle: 60,
    reconnectOnMount: true,
    // 子智能体消息保留在独立流中，避免混入 Supervisor 主对话。
    filterSubagentMessages: true,
    defaultHeaders: authHeaders,
    // streamSubgraphs 也会传回内部中间件子图状态。声明一个接收 state 的
    // onFinish 会让 SDK 在运行结束后重新读取主图 thread head，防止子图
    // values 成为最后一个本地快照时把主对话清空。
    onFinish: (state: unknown) => {
      void state;
    },
    onThreadId: (id: string) => {
      setThreadId(id);
      const url = new URL(window.location.href);
      url.searchParams.set("thread", id);
      window.history.replaceState({}, "", url);
    },
  };
  const baseStream = useStream<StreamState, { InterruptType: HITLRequest }>(streamOptions);
  const stream = baseStream as typeof baseStream & {
    subagents: Map<string, SubagentTraceStream>;
  };

  const liveMessages = stream.messages as Message[];
  const liveValues = stream.values as StreamState;
  const cachedMainSnapshot = mainSnapshotRef.current;
  const holdMainSnapshot = stream.isLoading
    && liveMessages.length === 0
    && cachedMainSnapshot !== undefined
    && cachedMainSnapshot.threadId === threadId
    && cachedMainSnapshot.messages.length > 0;
  const displayedMessages = holdMainSnapshot ? cachedMainSnapshot.messages : liveMessages;
  const displayedValues = holdMainSnapshot ? cachedMainSnapshot.values : liveValues;

  useLayoutEffect(() => {
    if (liveMessages.length === 0) return;
    // 只缓存含主对话消息的快照。内部中间件子图没有 messages，不能覆盖它。
    mainSnapshotRef.current = {
      threadId,
      messages: liveMessages,
      values: liveValues,
    };
  }, [liveMessages, liveValues, threadId]);

  // 轻量模式不构造工具卡和完整历史，避免每次流事件都扫描并挂载重型轨迹组件。
  const rows = useMemo(
    () => showDetails ? buildRows(displayedMessages) : EMPTY_ROWS,
    [displayedMessages, showDetails],
  );
  const compactTurn = useMemo(
    () => showDetails ? null : currentTurn(displayedMessages),
    [displayedMessages, showDetails],
  );
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
  const todos = Array.isArray(displayedValues?.todos) ? displayedValues.todos : [];
  const trackedTasks = useMemo(
    () => Object.values(displayedValues?.async_tasks ?? {}).reverse(),
    [displayedValues?.async_tasks],
  );
  const tasks = useMemo(
    () => trackedTasks.map((task) => {
      const live = polledTasks[task.task_id];
      if (!live || (live.run_id && task.run_id && live.run_id !== task.run_id)) return task;
      return { ...task, ...live };
    }),
    [polledTasks, trackedTasks],
  );
  const failedTasks = useMemo(
    () => tasks.filter(
      (task) => FAILED_ASYNC_TASK_STATUSES.has(task.status)
        && !dismissedTaskFailures.has(taskRunKey(task)),
    ),
    [dismissedTaskFailures, tasks],
  );
  const stopStream = useCallback(() => {
    void stream.stop();
  }, [stream.stop]);
  const runningTaskCount = tasks.filter(
    (task) => task.status === "running" || task.status === "pending",
  ).length;
  const pendingInterrupt = useMemo(
    () => hitlRequest(stream.interrupt?.value),
    [stream.interrupt?.value],
  );
  const latestPreparedDownload = useMemo(() => {
    for (let index = displayedMessages.length - 1; index >= 0; index -= 1) {
      const message = displayedMessages[index] as Message;
      if (message.type !== "tool") continue;
      const artifact = fileDownloadArtifact(message.artifact);
      if (artifact) {
        return {
          key: message.tool_call_id ?? message.id ?? `${artifact.path}-${index}`,
          artifact,
        };
      }
    }
    return null;
  }, [displayedMessages]);
  const identitySwitchBlocked = stream.isLoading
    || stream.queue.size > 0
    || runningTaskCount > 0
    || pendingInterrupt !== null
    || filesUploading;
  const filesReadyForAnalysis = !filesLoading
    && uploadedFiles.every((file) => file.status === "ready");
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

  const downloadArtifact = useCallback(async (
    artifact: DownloadableArtifact,
    mode: "auto" | "raw" | "bundle" = "auto",
  ) => {
    if (!threadId) throw new Error("请先打开包含该文件的会话");
    const markdown = artifact.path.toLowerCase().endsWith(".md");
    const bundle = mode === "bundle" || (mode === "auto" && markdown);
    const downloadKey = `${artifact.path}:${bundle ? "bundle" : "raw"}`;
    setDownloadingPath(downloadKey);
    setArtifactError("");
    try {
      const query = new URLSearchParams({ path: artifact.path });
      const response = await fetch(
        `${apiUrl}/artifacts/${encodeURIComponent(threadId)}/${bundle ? "bundle" : "download"}?${query.toString()}`,
        { headers: authHeaders },
      );
      if (!response.ok) {
        throw new Error(response.status === 401 ? "登录已失效，请重新登录" : "文件下载失败");
      }
      const blob = await response.blob();
      const objectUrl = window.URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = bundle
        ? `${artifact.filename.replace(/\.md$/i, "")}-bundle.zip`
        : artifact.filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => window.URL.revokeObjectURL(objectUrl), 0);
    } catch (error) {
      setArtifactError(error instanceof Error ? error.message : "文件下载失败");
      throw error;
    } finally {
      setDownloadingPath(undefined);
    }
  }, [apiUrl, authHeaders, threadId]);

  useEffect(() => {
    const controller = new AbortController();
    void loadSessions(controller.signal);
    return () => controller.abort();
  }, [loadSessions]);

  useEffect(() => {
    const controller = new AbortController();
    void loadArtifacts(controller.signal);
    return () => controller.abort();
  }, [loadArtifacts]);

  useEffect(() => {
    if (filesUploading) return undefined;
    const controller = new AbortController();
    void loadUploadedFiles(controller.signal);
    return () => controller.abort();
  }, [filesUploading, loadUploadedFiles]);

  useEffect(() => {
    if (!threadId || !latestPreparedDownload) return;
    if (!approvedSemanticDownloadRef.current) return;
    if (latestPreparedDownload.key === semanticDownloadBaselineRef.current) return;
    const storageKey = `deep-data-download:${threadId}:${latestPreparedDownload.key}`;
    if (window.sessionStorage.getItem(storageKey)) {
      approvedSemanticDownloadRef.current = false;
      semanticDownloadBaselineRef.current = null;
      return;
    }
    // 在发起请求前标记，避免密集流式更新触发重复浏览器下载。
    approvedSemanticDownloadRef.current = false;
    semanticDownloadBaselineRef.current = null;
    window.sessionStorage.setItem(storageKey, "1");
    void downloadArtifact(latestPreparedDownload.artifact).catch(() => undefined);
  }, [downloadArtifact, latestPreparedDownload, threadId]);

  useEffect(() => {
    if (previousLoadingRef.current && !stream.isLoading) {
      void loadSessions();
      void loadArtifacts();
    }
    previousLoadingRef.current = stream.isLoading;
  }, [loadArtifacts, loadSessions, stream.isLoading]);

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
    setDismissedTaskFailures(new Set());
    setArtifactError("");
    setFileError("");
    setInterruptError("");
    setVisibleRowLimit(INITIAL_VISIBLE_ROW_LIMIT);
    autoCollectedTaskRunsRef.current.clear();
    approvedSemanticDownloadRef.current = false;
    semanticDownloadBaselineRef.current = null;
    autoFollowRef.current = true;
    setShowJumpToBottom(false);
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
      { streamSubgraphs: true, streamResumable: true, onDisconnect: "continue" },
    )).catch(() => {
      runKeys.forEach((key) => autoCollectedTaskRunsRef.current.delete(key));
      setTaskRefreshError("任务已完成，但自动读取结果失败，请点击“读取结果”重试");
    });
  }, [stream.isLoading, stream.queue.size, stream.submit, tasks, trackedTasks]);

  useEffect(() => {
    const updateAutoFollow = () => {
      const atBottom = viewportAtBottom();
      autoFollowRef.current = atBottom;
      setShowJumpToBottom(!atBottom);
    };
    window.addEventListener("scroll", updateAutoFollow, { passive: true });
    window.addEventListener("resize", updateAutoFollow);
    return () => {
      window.removeEventListener("scroll", updateAutoFollow);
      window.removeEventListener("resize", updateAutoFollow);
    };
  }, []);

  useLayoutEffect(() => {
    if (!autoFollowRef.current) {
      setShowJumpToBottom(true);
      return;
    }
    // 只有用户原本位于底部时才跟随新内容；滚轮离开底部后保持阅读位置。
    scrollViewportToBottom();
    setShowJumpToBottom(false);
  }, [compactTurn?.assistantBody, compactTurn?.userKey, rows, showDetails, stream.isLoading]);

  useEffect(() => {
    if (!showDetails) return undefined;
    const conversation = conversationRef.current;
    if (!conversation || typeof ResizeObserver === "undefined") return undefined;
    // 子智能体过程会在 rows 不变时持续增高，监听真实布局变化才能保持贴底。
    const observer = new ResizeObserver(() => {
      if (!autoFollowRef.current) return;
      scrollViewportToBottom();
      setShowJumpToBottom(false);
    });
    observer.observe(conversation);
    return () => observer.disconnect();
  }, [showDetails]);

  async function createThreadForUpload(firstFilename: string): Promise<string> {
    const nextThreadId = window.crypto.randomUUID();
    const response = await fetch(`${apiUrl}/threads`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders },
      body: JSON.stringify({
        thread_id: nextThreadId,
        metadata: {
          graph_id: assistantId,
          kind: "conversation",
          title: `分析文件：${firstFilename}`,
        },
      }),
    });
    if (!response.ok) {
      throw new Error(response.status === 401 ? "登录已失效，请重新登录" : "创建文件分析会话失败");
    }
    stream.switchThread(nextThreadId);
    setThreadId(nextThreadId);
    const url = new URL(window.location.href);
    url.searchParams.set("thread", nextThreadId);
    window.history.replaceState({}, "", url);
    void loadSessions();
    return nextThreadId;
  }

  async function uploadOneFile(targetThreadId: string, item: UploadedTableFile): Promise<void> {
    if (!item.source) throw new Error("浏览器中已没有原始文件，请重新选择");
    const form = new FormData();
    form.append("files", item.source, item.name);
    const response = await fetch(`${apiUrl}/files/${encodeURIComponent(targetThreadId)}`, {
      method: "POST",
      headers: authHeaders,
      body: form,
    });
    const payload = await response.json().catch(() => ({})) as FileListResponse & { detail?: string };
    const uploaded = payload.files?.[0];
    if (!response.ok || !uploaded?.path) {
      throw new Error(payload.detail || "文件上传失败，请稍后重试");
    }
    setUploadedFiles((current) => current.map((file) => (
      file.key === item.key
        ? { ...uploaded, key: item.key, status: "ready" as const }
        : file
    )));
  }

  async function onFilesSelected(event: ChangeEvent<HTMLInputElement>) {
    const selected = Array.from(event.target.files ?? []);
    event.target.value = "";
    if (selected.length === 0) return;
    setFileError("");

    const currentReady = uploadedFiles.filter((file) => file.status === "ready");
    if (currentReady.length + selected.length > MAX_UPLOAD_FILES) {
      setFileError("当前会话最多保留 5 个上传文件");
      return;
    }
    const names = new Set(currentReady.map((file) => file.name.toLocaleLowerCase()));
    let selectedTotal = 0;
    for (const file of selected) {
      const folded = file.name.toLocaleLowerCase();
      if (!TABLE_FILE_PATTERN.test(file.name)) {
        setFileError(`仅支持 CSV、TSV 和 XLSX 文件：${file.name}`);
        return;
      }
      if (file.size > MAX_UPLOAD_FILE_BYTES) {
        setFileError(`单个文件不能超过 50 MB：${file.name}`);
        return;
      }
      if (names.has(folded)) {
        setFileError(`文件已存在或本次重复选择：${file.name}`);
        return;
      }
      names.add(folded);
      selectedTotal += file.size;
    }
    const currentTotal = currentReady.reduce((total, file) => total + file.size, 0);
    if (currentTotal + selectedTotal > MAX_UPLOAD_TOTAL_BYTES) {
      setFileError("当前会话上传文件总计不能超过 100 MB");
      return;
    }

    const pending: UploadedTableFile[] = selected.map((file) => ({
      key: window.crypto.randomUUID(),
      name: file.name,
      size: file.size,
      status: "uploading",
      source: file,
    }));
    setUploadedFiles((current) => [...current, ...pending]);
    setFilesUploading(true);
    let targetThreadId = threadId;
    try {
      targetThreadId ??= await createThreadForUpload(selected[0].name);
      for (const item of pending) {
        try {
          await uploadOneFile(targetThreadId, item);
        } catch (error) {
          const message = error instanceof Error ? error.message : "文件上传失败，请稍后重试";
          setUploadedFiles((current) => current.map((file) => (
            file.key === item.key ? { ...file, status: "error", error: message } : file
          )));
          setFileError(message);
        }
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : "创建文件分析会话失败";
      const failedKeys = new Set(pending.map((file) => file.key));
      setUploadedFiles((current) => current.map((file) => (
        failedKeys.has(file.key) ? { ...file, status: "error", error: message } : file
      )));
      setFileError(message);
    } finally {
      setFilesUploading(false);
    }
  }

  async function retryUpload(item: UploadedTableFile) {
    if (!threadId || !item.source || filesUploading) return;
    setFilesUploading(true);
    setFileError("");
    setUploadedFiles((current) => current.map((file) => (
      file.key === item.key ? { ...file, status: "uploading", error: undefined } : file
    )));
    try {
      await uploadOneFile(threadId, item);
    } catch (error) {
      const message = error instanceof Error ? error.message : "文件上传失败，请稍后重试";
      setUploadedFiles((current) => current.map((file) => (
        file.key === item.key ? { ...file, status: "error", error: message } : file
      )));
      setFileError(message);
    } finally {
      setFilesUploading(false);
    }
  }

  async function removeUploadedFile(item: UploadedTableFile) {
    if (filesUploading) return;
    if (!item.path || item.status === "error") {
      setUploadedFiles((current) => current.filter((file) => file.key !== item.key));
      return;
    }
    if (!threadId) return;
    setFilesUploading(true);
    setFileError("");
    setUploadedFiles((current) => current.map((file) => (
      file.key === item.key ? { ...file, status: "deleting" } : file
    )));
    try {
      const query = new URLSearchParams({ path: item.path });
      const response = await fetch(
        `${apiUrl}/files/${encodeURIComponent(threadId)}?${query.toString()}`,
        { method: "DELETE", headers: authHeaders },
      );
      const payload = await response.json().catch(() => ({})) as { detail?: string };
      if (!response.ok) throw new Error(payload.detail || "删除上传文件失败");
      setUploadedFiles((current) => current.filter((file) => file.key !== item.key));
    } catch (error) {
      const message = error instanceof Error ? error.message : "删除上传文件失败";
      setUploadedFiles((current) => current.map((file) => (
        file.key === item.key ? { ...file, status: "ready", error: message } : file
      )));
      setFileError(message);
    } finally {
      setFilesUploading(false);
    }
  }

  function submitText(text: string, mode: SubmitMode = "enqueue") {
    const value = text.trim();
    if (!value || !filesReadyForAnalysis) return;
    const uploadedPaths = uploadedFiles
      .filter((file): file is UploadedTableFile & { path: string } => file.status === "ready" && Boolean(file.path))
      .map((file) => `- ${file.path}`);
    const message = uploadedPaths.length > 0
      ? `${value}\n\n已上传文件：\n${uploadedPaths.join("\n")}`
      : value;
    const optimisticMessage = new HumanMessage({
      id: `optimistic-${window.crypto.randomUUID()}`,
      content: message,
    });
    setInput("");
    const multitaskOptions = stream.isLoading
      ? { multitaskStrategy: mode }
      : {};

    void stream.submit(
      { messages: [{ type: "human", content: message }] },
      {
        ...multitaskOptions,
        // LangGraph 会在首个图节点完成后才返回主图 values。先本地插入用户消息，
        // 避免新会话在模型思考或进入子图期间重新显示空首页。
        optimisticValues: (current) => ({
          messages: [...(current.messages ?? []), optimisticMessage],
        }),
        ...(!threadId && stream.messages.length === 0 ? {
          metadata: {
            kind: "conversation",
            title: conversationTitle(value),
          },
        } : {}),
        streamResumable: true,
        streamSubgraphs: true,
        onDisconnect: "continue",
      },
    );
  }

  async function resumeInterrupt(decisions: HITLDecision[]) {
    setInterruptSubmitting(true);
    setInterruptError("");
    approvedSemanticDownloadRef.current = decisions.some(
      (decision) => decision.type === "approve",
    );
    semanticDownloadBaselineRef.current = approvedSemanticDownloadRef.current
      ? latestPreparedDownload?.key ?? null
      : null;
    try {
      await stream.submit(null, {
        command: { resume: { decisions } },
        streamResumable: true,
        streamSubgraphs: true,
        onDisconnect: "continue",
      });
    } catch (error) {
      approvedSemanticDownloadRef.current = false;
      semanticDownloadBaselineRef.current = null;
      setInterruptError(error instanceof Error ? error.message : "暂时无法恢复任务");
    } finally {
      setInterruptSubmitting(false);
    }
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

  function jumpToBottom() {
    autoFollowRef.current = true;
    setShowJumpToBottom(false);
    scrollViewportToBottom();
  }

  function startNewThread() {
    const activeWorkCount = runningTaskCount
      + stream.queue.size
      + Number(stream.isLoading)
      + Number(pendingInterrupt !== null);
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
    setUploadedFiles([]);
    setFileError("");
    window.history.replaceState({}, "", window.location.pathname);
  }

  function selectSession(nextThreadId: string) {
    if (nextThreadId === threadId || stream.isLoading || stream.queue.size > 0 || filesUploading) return;
    stream.switchThread(nextThreadId);
    setThreadId(nextThreadId);
    setInput("");
    setUploadedFiles([]);
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
        setUploadedFiles([]);
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
    setUploadedFiles([]);
    setFileError("");
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
    <div className={`workspace-shell${showDetails ? "" : " workspace-shell--compact"}`}>
      <aside className="sidebar">
        <div className="brand">
          <BrandMark />
          <div>
            <strong>深研</strong>
            <span>网页与文件数据工作台</span>
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
          switchingDisabled={stream.isLoading || stream.queue.size > 0 || filesUploading}
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
          <div className="account-card__actions">
            {authUser.is_default ? (
              <>
                <button type="button" disabled={identitySwitchBlocked} onClick={() => openAuth("login")}>登录</button>
                <button type="button" disabled={identitySwitchBlocked} onClick={() => openAuth("register")}>注册</button>
              </>
            ) : null}
            <button
              className={`account-card__mode-toggle${authUser.is_default ? " account-card__mode-toggle--wide" : ""}`}
              type="button"
              role="switch"
              aria-checked={showDetails}
              onClick={() => setShowDetails((current) => !current)}
            >
              <span>详细模式</span>
              <i aria-hidden="true"><b /></i>
            </button>
            {!authUser.is_default ? (
              <button className="account-card__logout" type="button" disabled={identitySwitchBlocked} onClick={() => void logout()}>
                注销
              </button>
            ) : null}
          </div>
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
            <h1>从网页与文件到可追溯结论</h1>
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

        <section ref={conversationRef} className="conversation" aria-label="研究对话">
          {(showDetails ? rows.length === 0 : compactTurn === null) ? (
            <div className="empty-state">
              <p className="empty-state__index">研究入口 / 01</p>
              <h2>把一个问题，变成一条证据链。</h2>
              <p className="empty-state__lead">
                描述目标、网址，或上传 CSV、TSV、XLSX。Supervisor 会规划、验证并整理成可核验的分析结果。
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

          {showDetails && hiddenRowCount > 0 ? (
            <button
              type="button"
              className="load-earlier"
              onClick={() => setVisibleRowLimit((current) => current + INITIAL_VISIBLE_ROW_LIMIT)}
            >
              加载更早记录（还有 {hiddenRowCount} 条）
            </button>
          ) : null}

          {showDetails ? visibleRows.map((row) =>
            row.kind === "tool" ? (
              <ToolCallCard
                key={row.key}
                card={row.card}
                subagent={row.card.name === "task" ? stream.subagents.get(row.card.callId) : undefined}
              />
            ) : (
              <MessageCard
                key={row.key}
                messageKey={row.key}
                role={row.role}
                body={row.body}
                report={row.report}
                streaming={stream.isLoading && row.role === "ai" && row.key === lastMessageKey}
                apiUrl={apiUrl}
                authHeaders={authHeaders}
                threadId={threadId}
              />
            ),
          ) : compactTurn ? (
            <CompactTurnView
              {...compactTurn}
              streaming={stream.isLoading}
              apiUrl={apiUrl}
              authHeaders={authHeaders}
              threadId={threadId}
              onStop={stopStream}
            />
          ) : null}

          {failedTasks.length > 0 ? (
            <section className="task-failure-alert" role="alert" aria-live="assertive">
              <header>
                <div>
                  <p className="eyebrow">后台采集异常</p>
                  <h2>有 {failedTasks.length} 个任务未正常完成</h2>
                </div>
                <button
                  type="button"
                  aria-label="关闭后台任务失败提醒"
                  onClick={() => setDismissedTaskFailures((current) => {
                    const next = new Set(current);
                    failedTasks.forEach((task) => next.add(taskRunKey(task)));
                    return next;
                  })}
                >
                  知道了
                </button>
              </header>
              <ul>
                {failedTasks.map((task) => (
                  <li key={taskRunKey(task)}>
                    <strong>
                      {task.agent_name ?? "crawl-worker"} · {TASK_FAILURE_LABEL[task.status]}
                    </strong>
                    <code>{task.task_id}</code>
                    <span>{task.error_summary ?? "子任务未正常完成，请检查任务详情。"}</span>
                  </li>
                ))}
              </ul>
              <p>系统不会自动原样重试，请先根据错误原因调整任务或服务配置。</p>
            </section>
          ) : null}

          {pendingInterrupt ? (
            <InterruptCard
              key={stream.interrupt?.id ?? "current-interrupt"}
              request={pendingInterrupt}
              submitting={interruptSubmitting}
              onResume={(decisions) => void resumeInterrupt(decisions)}
            />
          ) : null}

          {interruptError ? (
            <div className="error-card" role="alert">
              <strong>无法恢复任务</strong>
              <span>{interruptError}</span>
            </div>
          ) : null}

          {artifactsLoading || artifacts.length > 0 || artifactError ? (
            <section className="artifact-card" aria-labelledby="artifact-title">
              <header>
                <div>
                  <p className="eyebrow">当前会话</p>
                  <h2 id="artifact-title">研究产物</h2>
                </div>
                {artifactsLoading ? <span>正在同步…</span> : null}
              </header>
              {artifacts.length > 0 ? (
                <ul>
                  {artifacts.map((artifact) => (
                    <li key={artifact.path}>
                      <div>
                        <strong>{artifact.filename}</strong>
                        <span>{formatFileSize(artifact.size)} · {artifact.path}</span>
                      </div>
                      <div className="artifact-card__actions">
                        {artifact.path.toLowerCase().endsWith(".md") ? (
                          <button
                            type="button"
                            disabled={downloadingPath === `${artifact.path}:bundle`}
                            onClick={() => void downloadArtifact(artifact, "bundle").catch(() => undefined)}
                          >
                            {downloadingPath === `${artifact.path}:bundle` ? "打包中…" : "下载 ZIP"}
                          </button>
                        ) : (
                          <button
                            type="button"
                            disabled={downloadingPath === `${artifact.path}:raw`}
                            onClick={() => void downloadArtifact(artifact, "raw").catch(() => undefined)}
                          >
                            {downloadingPath === `${artifact.path}:raw` ? "下载中…" : "下载"}
                          </button>
                        )}
                      </div>
                    </li>
                  ))}
                </ul>
              ) : null}
              {artifactError ? <p className="artifact-card__error">{artifactError}</p> : null}
            </section>
          ) : null}

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

          {showDetails && stream.isLoading ? (
            <div className="working-card" role="status" aria-live="polite">
              <div className="working-card__signal" aria-hidden="true"><i /><i /><i /></div>
              <div>
                <strong>正在推进研究任务</strong>
                <span>规划、工具调用和结果会实时出现在这里。</span>
              </div>
              <button type="button" onClick={stopStream}>停止回答</button>
            </div>
          ) : null}

          {stream.error ? (
            <div className="error-card" role="alert">
              <strong>运行失败</strong>
              <span>{String(stream.error)}</span>
            </div>
          ) : null}
        </section>

        {showJumpToBottom && (showDetails ? rows.length > 0 : compactTurn !== null) ? (
          <button
            type="button"
            className="jump-to-bottom"
            onClick={jumpToBottom}
            aria-label="回到对话底部"
          >
            <span aria-hidden="true">↓</span>
            回到底部
          </button>
        ) : null}

        <form className={`composer${showDetails ? "" : " composer--compact"}`} onSubmit={onSubmit}>
          <div className="composer__field">
            <label htmlFor="research-input">
              {pendingInterrupt
                ? "请先处理上方待确认事项"
                : stream.isLoading
                  ? "补充要求或纠正方向"
                  : "描述你的网页或文件分析任务"}
            </label>
            <div className="composer__attachments">
              <input
                ref={fileInputRef}
                type="file"
                accept=".csv,.tsv,.xlsx"
                multiple
                onChange={(event) => void onFilesSelected(event)}
                disabled={identitySwitchBlocked}
                aria-label="选择本地表格文件"
              />
              <button
                type="button"
                className="attachment-button"
                disabled={identitySwitchBlocked || uploadedFiles.filter((file) => file.status === "ready").length >= MAX_UPLOAD_FILES}
                onClick={() => fileInputRef.current?.click()}
              >
                <span aria-hidden="true">＋</span>
                添加 CSV / TSV / XLSX
              </button>
              {filesLoading ? <small>正在恢复附件…</small> : null}
            </div>
            {uploadedFiles.length > 0 ? (
              <ul className="attachment-list" aria-label="已选择的表格文件">
                {uploadedFiles.map((file) => (
                  <li key={file.key} className={`attachment-list__item is-${file.status}`}>
                    <div>
                      <strong title={file.name}>{file.name}</strong>
                      <span>
                        {formatFileSize(file.size)} · {
                          file.status === "ready"
                            ? "已上传"
                            : file.status === "uploading"
                              ? "上传中"
                              : file.status === "deleting"
                                ? "删除中"
                                : "上传失败"
                        }
                      </span>
                      {file.error ? <small>{file.error}</small> : null}
                    </div>
                    <div className="attachment-list__actions">
                      {file.status === "error" && file.source ? (
                        <button type="button" disabled={filesUploading} onClick={() => void retryUpload(file)}>
                          重试
                        </button>
                      ) : null}
                      <button
                        type="button"
                        disabled={filesUploading}
                        onClick={() => void removeUploadedFile(file)}
                        aria-label={`删除附件：${file.name}`}
                      >
                        删除
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            ) : null}
            {fileError ? <p className="composer__file-error" role="alert">{fileError}</p> : null}
            <textarea
              id="research-input"
              rows={3}
              value={input}
              onChange={(event) => setInput(event.target.value)}
              onKeyDown={onComposerKeyDown}
              placeholder="例如：分析已上传订单表的月度趋势和异常值，并生成图表……"
              disabled={pendingInterrupt !== null}
            />
            <span>
              {pendingInterrupt
                ? "任务已暂停，提交决定后将从原位置继续"
                : stream.isLoading
                ? "Enter 排队发送 · Shift + Enter 换行"
                : "Enter 发送 · Shift + Enter 换行"}
            </span>
          </div>
          <div className="composer__actions">
            <button
              className="send-button"
              type="submit"
              disabled={!input.trim() || pendingInterrupt !== null || !filesReadyForAnalysis}
              aria-label={stream.isLoading ? "排队发送消息" : "发送分析任务"}
            >
              <span>{stream.isLoading ? "排队发送" : "发送任务"}</span>
              <i aria-hidden="true">↗</i>
            </button>
            {stream.isLoading && !pendingInterrupt ? (
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

      {showDetails ? (
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
          <SubagentPlanPanel subagents={stream.subagents} />
        </aside>
      ) : null}

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
