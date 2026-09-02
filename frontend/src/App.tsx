import { ChangeEvent, CSSProperties, FormEvent, ImgHTMLAttributes, KeyboardEvent, memo, PointerEvent as ReactPointerEvent, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { HumanMessage } from "@langchain/core/messages";
import { Client } from "@langchain/langgraph-sdk/client";
import { useStream } from "@langchain/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useThreadRunManager } from "./features/chat/useThreadRunManager";
import SessionHistory, { type ConversationThread } from "./features/threads/SessionHistory";
import SubagentPlanPanel from "./features/tasks/SubagentPlanPanel";
import TodoPanel, { type TodoItem } from "./features/tasks/TodoPanel";
import type { SubagentTraceStream } from "./features/traces/SubagentTrace";
import TaskTrace, { type AsyncTask, type AsyncTaskStatus } from "./features/traces/TaskTrace";
import ToolCallCard, { type ToolCard } from "./features/traces/ToolCallCard";

type RawToolCall = { id?: string; name?: string; args?: unknown };
type Message = {
  id?: string;
  name?: string;
  type: string;
  content: unknown;
  status?: string;
  tool_calls?: RawToolCall[];
  tool_call_id?: string;
  artifact?: unknown;
};

type StreamState = {
  messages: Message[];
  todos?: TodoItem[];
  async_tasks?: Record<string, AsyncTask>;
  context_usage?: ContextUsageSnapshot | null;
};

export type ContextUsageSnapshot = {
  used_tokens: number;
  max_input_tokens: number;
  provider_version: number;
};
export type ContextUsageEvent = ContextUsageSnapshot & {
  type: "context_usage";
  phase: "before_model" | "after_model";
};

type WebSearchSource = { title: string; url: string };
type WebSearchAction = {
  type: "search" | "open_page" | "find_in_page";
  query?: string;
  queries?: string[];
  url?: string;
  pattern?: string;
};
export type WebSearchProgressEvent = {
  type: "web_search_progress";
  phase: "in_progress" | "searching" | "completed";
  item_id: string;
  output_index: number;
  sequence_number: number;
  action?: WebSearchAction;
  sources?: WebSearchSource[];
};
type AppCustomEvent = WebSearchProgressEvent | ContextUsageEvent;

type Row =
  | { kind: "message"; key: string; role: "human" | "ai"; body: string; report: boolean }
  | { kind: "tool"; key: string; card: ToolCard }
  | { kind: "web_search"; key: string; search: WebSearchProgressEvent };

type CompactActivityKind = "planning" | "streaming" | "tool" | "web_search" | "todo" | "synthesizing" | "complete";
type CompactActivity = {
  kind: CompactActivityKind;
  text: string;
  statusLabel: string;
};
type CompactToolCall = {
  id: string;
  name: string;
  args: unknown;
  completed: boolean;
};
type CompactTurnData = {
  userKey: string;
  userBody: string;
  assistantKey: string;
  assistantBody: string;
  assistantReport: boolean;
  lastEvent: "none" | "monitor" | "ai-text" | "ai-tool" | "tool";
  tools: CompactToolCall[];
};
type CompactTurn = CompactTurnData & {
  activity: CompactActivity;
};

type SubmitMode = "enqueue" | "interrupt";
type AuthMode = "login" | "register";
type AuthStatus = "checking" | "ready" | "required" | "unavailable";
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
type MemoryClearResponse = {
  status?: string;
  cancelled_jobs?: number;
  detail?: string;
};
type MemorySettingsResponse = {
  failure_lesson_saving_enabled?: boolean;
  cancelled_jobs?: number;
  detail?: string;
};
type ModelProvider = {
  provider_name: string;
  provider_type: "responses" | "chat_completions" | "anthropic";
  base_url: string;
  model_name: string;
  has_api_key: boolean;
  api_key_hint: string;
  version: number;
  updated_at: string;
};
type ModelProviderResponse = {
  configured?: boolean;
  provider?: ModelProvider | null;
  provider_name?: string;
  provider_type?: ModelProvider["provider_type"];
  deleted?: boolean;
  detail?: string | LimitDetail;
};
type ModelProviderDraft = Pick<ModelProvider, "base_url" | "model_name">;
type LimitDetail = {
  code: string;
  message: string;
  limit?: number;
  retry_after_seconds?: number;
  active_thread_ids?: string[];
  balance_tokens?: number;
  capacity_tokens?: number;
  refill_tokens_per_hour?: number;
  next_refill_at?: string;
};
type LimitDialog = LimitDetail & { retryUntil?: number };

class LangGraphApiError extends Error {
  readonly status: number;
  readonly detail?: LimitDetail;

  constructor(status: number, message: string, detail?: LimitDetail) {
    super(message);
    this.name = "LangGraphApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function langGraphFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const response = await fetch(input, init);
  if (response.ok) return response;
  const body = await response.clone().json().catch(() => null) as { detail?: LimitDetail | string } | null;
  const detail = typeof body?.detail === "object" ? body.detail : undefined;
  const message = detail?.message
    ?? (typeof body?.detail === "string" ? body.detail : `请求失败（${response.status}）`);
  throw new LangGraphApiError(response.status, message, detail);
}

const AUTH_TOKEN_KEY = "deep-data-auth-token";
const TASK_POLL_INTERVAL_MS = 4_000;
const INITIAL_VISIBLE_ROW_LIMIT = 60;
const EMPTY_ROWS: Row[] = [];
const MAX_UPLOAD_FILES = 5;
const MAX_UPLOAD_FILE_BYTES = 50 * 1024 * 1024;
const MAX_UPLOAD_TOTAL_BYTES = 100 * 1024 * 1024;
const TABLE_FILE_PATTERN = /\.(csv|tsv|xlsx)$/i;
const COMPOSER_TEXTAREA_MIN_HEIGHT = 54;
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

function safeHttpUrl(value: unknown): string | null {
  if (typeof value !== "string") return null;
  try {
    const url = new URL(value);
    return url.protocol === "http:" || url.protocol === "https:" ? url.toString() : null;
  } catch {
    return null;
  }
}

function annotatedTextMarkdown(text: string, annotations: unknown): string {
  if (!Array.isArray(annotations) || annotations.length === 0) return text;
  const characters = Array.from(text);
  const citations = annotations.flatMap((annotation) => {
    if (typeof annotation !== "object" || annotation === null) return [];
    const raw = annotation as Record<string, unknown>;
    if (raw.type !== "url_citation" && raw.type !== "citation") return [];
    const url = safeHttpUrl(raw.url);
    const start = raw.start_index;
    const end = raw.end_index;
    if (!url || !Number.isInteger(start) || !Number.isInteger(end)) return [];
    const startIndex = Number(start);
    const endIndex = Number(end);
    if (startIndex < 0 || endIndex <= startIndex || endIndex > characters.length) return [];
    return [{ startIndex, endIndex, url }];
  }).sort((left, right) => right.startIndex - left.startIndex);

  let rendered = characters;
  let previousStart = characters.length;
  for (const citation of citations) {
    if (citation.endIndex > previousStart) continue;
    const label = rendered
      .slice(citation.startIndex, citation.endIndex)
      .join("")
      .replace(/([\\\[\]])/g, "\\$1");
    rendered.splice(
      citation.startIndex,
      citation.endIndex - citation.startIndex,
      `[${label}](<${citation.url}>)`,
    );
    previousStart = citation.startIndex;
  }
  return rendered.join("");
}

export function messageMarkdown(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";
  return content.map((part) => {
    if (typeof part === "string") return part;
    if (typeof part !== "object" || part === null || !("text" in part)) return "";
    const raw = part as { text?: unknown; annotations?: unknown };
    return annotatedTextMarkdown(String(raw.text ?? ""), raw.annotations);
  }).join("");
}

function normalizedWebSearchSource(value: unknown): WebSearchSource | null {
  if (typeof value !== "object" || value === null) return null;
  const raw = value as Record<string, unknown>;
  const url = safeHttpUrl(raw.url);
  if (!url) return null;
  const title = typeof raw.title === "string" && raw.title.trim()
    ? raw.title.trim()
    : new URL(url).hostname;
  return { title, url };
}

export function normalizeWebSearchEvent(value: unknown): WebSearchProgressEvent | null {
  if (typeof value !== "object" || value === null) return null;
  const raw = value as Record<string, unknown>;
  const phases = new Set(["in_progress", "searching", "completed"]);
  if (
    raw.type !== "web_search_progress"
    || typeof raw.item_id !== "string"
    || !raw.item_id
    || !phases.has(String(raw.phase))
    || !Number.isInteger(raw.output_index)
    || !Number.isInteger(raw.sequence_number)
  ) return null;

  let action: WebSearchAction | undefined;
  if (typeof raw.action === "object" && raw.action !== null) {
    const source = raw.action as Record<string, unknown>;
    if (source.type === "search" || source.type === "open_page" || source.type === "find_in_page") {
      action = { type: source.type };
      if (typeof source.query === "string") action.query = source.query;
      if (Array.isArray(source.queries)) {
        action.queries = source.queries.filter((item): item is string => typeof item === "string");
      }
      if (typeof source.url === "string") action.url = source.url;
      if (typeof source.pattern === "string") action.pattern = source.pattern;
    }
  }
  const sources: WebSearchSource[] = [];
  const seen = new Set<string>();
  if (Array.isArray(raw.sources)) {
    for (const candidate of raw.sources) {
      const source = normalizedWebSearchSource(candidate);
      if (!source || seen.has(source.url)) continue;
      seen.add(source.url);
      sources.push(source);
      if (sources.length >= 20) break;
    }
  }
  return {
    type: "web_search_progress",
    phase: raw.phase as WebSearchProgressEvent["phase"],
    item_id: raw.item_id,
    output_index: Number(raw.output_index),
    sequence_number: Number(raw.sequence_number),
    ...(action ? { action } : {}),
    ...(sources.length > 0 ? { sources } : {}),
  };
}

function normalizeContextUsageSnapshot(value: unknown): ContextUsageSnapshot | null {
  if (typeof value !== "object" || value === null) return null;
  const raw = value as Record<string, unknown>;
  if (
    !Number.isFinite(raw.used_tokens)
    || !Number.isFinite(raw.max_input_tokens)
    || !Number.isInteger(raw.provider_version)
  ) return null;
  const usedTokens = Number(raw.used_tokens);
  const maxInputTokens = Number(raw.max_input_tokens);
  const providerVersion = Number(raw.provider_version);
  if (usedTokens < 0 || maxInputTokens <= 0 || providerVersion <= 0) return null;
  return {
    used_tokens: Math.round(usedTokens),
    max_input_tokens: Math.round(maxInputTokens),
    provider_version: providerVersion,
  };
}

export function normalizeContextUsageEvent(value: unknown): ContextUsageEvent | null {
  if (typeof value !== "object" || value === null) return null;
  const raw = value as Record<string, unknown>;
  if (
    raw.type !== "context_usage"
    || (raw.phase !== "before_model" && raw.phase !== "after_model")
  ) return null;
  const snapshot = normalizeContextUsageSnapshot(raw);
  return snapshot ? { type: "context_usage", phase: raw.phase, ...snapshot } : null;
}

function compactTokenCount(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1).replace(/\.0$/, "")}M`;
  if (value >= 1_000) return `${Math.round(value / 1_000)}k`;
  return String(value);
}

function ContextWindowIndicator({ usage }: { usage: ContextUsageSnapshot | null }) {
  if (!usage) return null;
  const rawPercent = (usage.used_tokens / usage.max_input_tokens) * 100;
  // Floor the display so warning colors begin only after the real threshold.
  const percent = Math.max(0, Math.floor(rawPercent));
  const ringPercent = Math.min(100, rawPercent);
  const tone = percent >= 85 ? "danger" : percent >= 70 ? "warning" : "normal";
  const label = `上下文窗口：${percent}% 已用，已用 ${compactTokenCount(usage.used_tokens)} Token，共 ${compactTokenCount(usage.max_input_tokens)}`;
  return (
    <span
      className={`context-window context-window--${tone}`}
      role="status"
      tabIndex={0}
      aria-label={label}
      style={{ "--context-percent": `${ringPercent}%` } as CSSProperties}
    >
      <span className="context-window__ring" aria-hidden="true" />
      <span className="context-window__tooltip" role="tooltip">
        <span>上下文窗口：</span>
        <span>{percent}% 已用</span>
        <strong>已用 {compactTokenCount(usage.used_tokens)} Token，共 {compactTokenCount(usage.max_input_tokens)}</strong>
      </span>
    </span>
  );
}

function persistedWebSearches(content: unknown): WebSearchProgressEvent[] {
  if (!Array.isArray(content)) return [];
  return content.flatMap((part, index) => {
    if (typeof part !== "object" || part === null) return [];
    const raw = part as Record<string, unknown>;
    if (raw.type !== "web_search_call" || typeof raw.id !== "string") return [];
    return [normalizeWebSearchEvent({
      type: "web_search_progress",
      phase: "completed",
      item_id: raw.id,
      output_index: Number.isInteger(raw.index) ? raw.index : index,
      sequence_number: Number.MAX_SAFE_INTEGER,
      action: raw.action,
      sources: typeof raw.action === "object" && raw.action !== null
        ? (raw.action as Record<string, unknown>).sources
        : undefined,
    })].filter((item): item is WebSearchProgressEvent => item !== null);
  });
}

function providerErrorMessage(body: ModelProviderResponse, fallback: string): string {
  if (typeof body.detail === "string") return body.detail;
  if (body.detail && typeof body.detail.message === "string") return body.detail.message;
  return fallback;
}

function reconcileSubagentStatuses(
  subagents: Map<string, SubagentTraceStream>,
  messages: Message[],
): Map<string, SubagentTraceStream> {
  const completedCalls = new Map<string, { error: boolean; result: string }>();
  for (const message of messages) {
    if (message.type !== "tool" || !message.tool_call_id) continue;
    completedCalls.set(message.tool_call_id, {
      error: message.status === "error",
      result: messageText(message.content),
    });
  }
  if (completedCalls.size === 0) return subagents;

  let changed = false;
  const reconciled = new Map<string, SubagentTraceStream>();
  for (const [id, subagent] of subagents) {
    const completion = completedCalls.get(id);
    if (!completion || subagent.status === "complete" || subagent.status === "error") {
      reconciled.set(id, subagent);
      continue;
    }
    changed = true;
    reconciled.set(id, {
      ...subagent,
      status: completion.error ? "error" : "complete",
      isLoading: false,
      result: completion.error ? null : completion.result,
      error: completion.error ? completion.result : null,
      completedAt: subagent.completedAt ?? new Date(),
    });
  }
  return changed ? reconciled : subagents;
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
      if (message.type === "ai") {
        for (const search of persistedWebSearches(message.content)) {
          rows.push({ kind: "web_search", key: `web-search-${search.item_id}`, search });
        }
      }
      const body = messageMarkdown(message.content).trim();
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

export function buildCompactTurns(messages: Message[]): CompactTurnData[] {
  const turns: CompactTurnData[] = [];
  const toolOwners = new Map<string, CompactToolCall>();
  let current: CompactTurnData | undefined;

  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index];
    if (message.type === "human") {
      if (message.name === "async-task-monitor") {
        if (current) current.lastEvent = "monitor";
        continue;
      }
      const userBody = messageMarkdown(message.content).trim();
      if (!userBody) continue;
      const userKey = message.id ?? `human-${index}`;
      current = {
        userKey,
        userBody,
        assistantKey: `assistant-${userKey}`,
        assistantBody: "",
        assistantReport: false,
        lastEvent: "none",
        tools: [],
      };
      turns.push(current);
      continue;
    }
    if (!current) continue;

    if (message.type === "ai") {
      const body = messageMarkdown(message.content).trim();
      if (body) {
        // 简略历史只保留本轮最后一条非空 AI 回复，不合并中间说明。
        current.assistantKey = message.id ?? `assistant-${current.userKey}-${index}`;
        current.assistantBody = body;
        current.assistantReport = isReport(body);
      }
      const calls = message.tool_calls ?? [];
      current.lastEvent = calls.length > 0 ? "ai-tool" : body ? "ai-text" : current.lastEvent;
      for (const call of calls) {
        const toolCall: CompactToolCall = {
          id: call.id ?? `${message.id ?? current.userKey}-${call.name ?? "tool"}-${index}`,
          name: call.name ?? "tool",
          args: call.args ?? {},
          completed: false,
        };
        current.tools.push(toolCall);
        toolOwners.set(toolCall.id, toolCall);
      }
      continue;
    }

    if (message.type === "tool") {
      if (message.tool_call_id) {
        const owner = toolOwners.get(message.tool_call_id);
        if (owner) owner.completed = true;
      }
      current.lastEvent = "tool";
    }
  }

  return turns;
}

function withoutQueuedTurns(turns: CompactTurnData[], queuedBodies: string[]): CompactTurnData[] {
  if (queuedBodies.length === 0 || turns.length === 0) return turns;
  const visible = [...turns];
  // enqueue 的 optimisticValues 可能把等待消息临时放进主状态；尾部精确匹配后
  // 交给现有队列卡展示，避免把它误判成当前正在执行的轮次。
  for (let index = queuedBodies.length - 1; index >= 0; index -= 1) {
    if (visible.at(-1)?.userBody !== queuedBodies[index]) break;
    visible.pop();
  }
  return visible;
}

function compactToolActivity(toolCall: CompactToolCall): string {
  const args = typeof toolCall.args === "object" && toolCall.args !== null
    ? toolCall.args as Record<string, unknown>
    : {};
  if (toolCall.name === "task") {
    const agent = typeof args.subagent_type === "string" ? args.subagent_type : "同步子智能体";
    return `正在调用 ${agent}…`;
  }
  if (toolCall.name === "start_async_task") {
    const agent = typeof args.subagent_type === "string" ? args.subagent_type : "后台智能体";
    return `正在启动 ${agent}…`;
  }
  const labels: Record<string, string> = {
    write_todos: "正在更新研究计划…",
    read_file: "正在读取研究产物…",
    write_file: "正在写入研究产物…",
    edit_file: "正在编辑研究产物…",
    execute: "正在执行分析脚本…",
    database_list_schemas: "正在读取数据库结构…",
    database_list_objects: "正在读取数据库对象…",
    database_get_object_details: "正在检查数据库字段…",
    database_query_preview: "正在查询数据库…",
    database_query_to_file: "正在导出数据库结果…",
    tavily_search: "正在搜索网页…",
    tavily_crawl: "正在采集网页…",
    tavily_extract: "正在提取网页内容…",
    request_report_download: "正在准备报告下载…",
    send_report_email: "正在发送报告邮件…",
  };
  return labels[toolCall.name] ?? `正在执行 ${toolCall.name}…`;
}

function compactActivity(
  turn: CompactTurnData,
  options: {
    active: boolean;
    latest: boolean;
    todos: TodoItem[];
    interrupted: boolean;
    failed: boolean;
    webSearch?: WebSearchProgressEvent;
  },
): CompactActivity {
  if (!options.active) {
    if (turn.assistantBody) return { kind: "complete", text: turn.assistantBody, statusLabel: "已完成" };
    if (options.latest && options.interrupted) {
      return { kind: "complete", text: "任务已暂停，等待你的确认。", statusLabel: "已暂停" };
    }
    if (options.latest && options.failed) {
      return { kind: "complete", text: "本轮执行失败，未产生最终回复。", statusLabel: "失败" };
    }
    return { kind: "complete", text: "本轮未产生最终回复。", statusLabel: "未完成" };
  }

  // 只有最后事件仍是纯 AI 文本时，才把最后回复视为当前正在生成的文本；
  // 带工具调用的 AI 消息已经结束生成，应转而展示工具执行状态。
  if (turn.lastEvent === "ai-text" && turn.assistantBody) {
    return { kind: "streaming", text: turn.assistantBody, statusLabel: "生成中" };
  }
  if (options.webSearch) {
    const query = options.webSearch.action?.query ?? options.webSearch.action?.queries?.[0];
    if (options.webSearch.phase === "completed") {
      return { kind: "web_search", text: "网页搜索完成，正在整理结果…", statusLabel: "整理中" };
    }
    return {
      kind: "web_search",
      text: query ? `正在搜索网页：${query}` : "正在搜索网页…",
      statusLabel: "搜索中",
    };
  }
  const pendingTool = [...turn.tools].reverse().find((toolCall) => !toolCall.completed);
  if (pendingTool) {
    return { kind: "tool", text: compactToolActivity(pendingTool), statusLabel: "执行中" };
  }
  const activeTodo = options.todos.find((todo) => todo.status === "in_progress");
  if (activeTodo) {
    return { kind: "todo", text: `正在${activeTodo.content.replace(/^正在/, "")}…`, statusLabel: "执行中" };
  }
  if (turn.lastEvent === "tool" || turn.tools.length > 0) {
    return { kind: "synthesizing", text: "正在整理工具结果…", statusLabel: "整理中" };
  }
  if (turn.lastEvent === "monitor") {
    return { kind: "planning", text: "正在读取后台任务结果…", statusLabel: "执行中" };
  }
  return { kind: "planning", text: "正在规划任务…", statusLabel: "规划中" };
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

function webSearchDescription(search: WebSearchProgressEvent): string {
  const action = search.action;
  if (action?.type === "search") {
    return action.query ?? action.queries?.join("；") ?? "正在检索公开网页";
  }
  if (action?.type === "open_page") return action.url ?? "正在打开搜索结果";
  if (action?.type === "find_in_page") {
    return action.pattern ? `页内查找：${action.pattern}` : action.url ?? "正在查找页面内容";
  }
  return search.phase === "completed" ? "网页搜索已完成" : "正在搜索网页";
}

function webSearchSourceLabel(source: { title: string; url: string }): string {
  try {
    return new URL(source.url).hostname.replace(/^www\./, "");
  } catch {
    return source.title;
  }
}

const WebSearchCard = memo(function WebSearchCard({
  search,
  active = false,
}: {
  search: WebSearchProgressEvent;
  active?: boolean;
}) {
  const complete = search.phase === "completed";
  const status = complete ? (active ? "正在整理" : "已完成") : "搜索中";
  return (
    <section
      className={`web-search-card${complete ? " is-complete" : " is-searching"}`}
      aria-live={active ? "polite" : undefined}
    >
      <svg className="web-search-card__icon" viewBox="0 0 24 24" aria-hidden="true">
        <circle cx="12" cy="12" r="8.25" />
        <path d="M3.75 12h16.5M12 3.75c2.1 2.25 3.15 5 3.15 8.25S14.1 18 12 20.25C9.9 18 8.85 15.25 8.85 12S9.9 6 12 3.75Z" />
      </svg>
      <strong>{complete ? "已搜索网页" : "正在搜索网页"}</strong>
      <div className="web-search-card__content">
        <span className="web-search-card__description">{webSearchDescription(search)}</span>
        {search.sources?.length ? (
          <span className="web-search-card__sources" aria-label="网页搜索来源">
            <span aria-hidden="true">|</span>
            {search.sources.map((source, index) => (
              <span className="web-search-card__source" key={source.url}>
                {index > 0 ? <span aria-hidden="true"> · </span> : null}
                <a
                  href={source.url}
                  title={source.title}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  {webSearchSourceLabel(source)}
                </a>
              </span>
            ))}
          </span>
        ) : null}
      </div>
      <span className="web-search-card__status">{status}</span>
    </section>
  );
});

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
              a: (props) => <a {...props} target="_blank" rel="noopener noreferrer" />,
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

type CompactTurnViewProps = {
  turn: CompactTurn;
  active: boolean;
  apiUrl: string;
  authHeaders: Record<string, string>;
  threadId?: string;
  onStop: () => void;
};
const DEFAULT_PROVIDER_DRAFT: ModelProviderDraft = {
  base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1",
  model_name: "qwen-plus",
};
const PROVIDER_TYPE_LABELS: Record<ModelProvider["provider_type"], string> = {
  responses: "Responses API",
  chat_completions: "Chat Completions",
  anthropic: "Anthropic Native",
};
const SIGNED_OUT_USER: AuthUser = {
  id: "",
  username: "未登录",
  is_default: false,
};

const CompactTurnView = memo(function CompactTurnView({
  turn,
  active,
  apiUrl,
  authHeaders,
  threadId,
  onStop,
}: CompactTurnViewProps) {
  const streamingText = turn.activity.kind === "streaming";
  const finalReply = turn.activity.kind === "complete" && Boolean(turn.assistantBody);
  return (
    <section className={`compact-turn${active ? " compact-turn--active" : ""}`}>
      <MessageCard
        messageKey={turn.userKey}
        role="human"
        body={turn.userBody}
        report={false}
        streaming={false}
        apiUrl={apiUrl}
        authHeaders={authHeaders}
        threadId={threadId}
      />
      <article className={`message message--ai compact-turn__output${turn.assistantReport && finalReply ? " message--report" : ""}`}>
        <header>
          <span>Supervisor</span>
          <i aria-hidden="true" />
          <span className={`compact-turn__status${active ? " is-running" : ""}`}>
            {turn.activity.statusLabel}
          </span>
          {active ? (
            <button type="button" onClick={onStop}>停止回答</button>
          ) : null}
        </header>
        <div
          className={`markdown-body${streamingText ? " markdown-body--streaming" : ""}${finalReply || streamingText ? "" : " compact-turn__activity"}`}
          aria-live="polite"
        >
          {finalReply ? (
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
                a: (props) => <a {...props} target="_blank" rel="noopener noreferrer" />,
              }}
            >
              {turn.assistantBody}
            </ReactMarkdown>
          ) : turn.activity.text}
        </div>
      </article>
    </section>
  );
}, (previous, next) => (
  previous.turn.userKey === next.turn.userKey
  && previous.turn.userBody === next.turn.userBody
  && previous.turn.assistantKey === next.turn.assistantKey
  && previous.turn.assistantBody === next.turn.assistantBody
  && previous.turn.assistantReport === next.turn.assistantReport
  && previous.turn.activity.kind === next.turn.activity.kind
  && previous.turn.activity.text === next.turn.activity.text
  && previous.turn.activity.statusLabel === next.turn.activity.statusLabel
  && previous.active === next.active
  && previous.apiUrl === next.apiUrl
  && previous.authHeaders === next.authHeaders
  && previous.threadId === next.threadId
));

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
        const isEmail = action.name === "send_report_email";
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
        const recipient = typeof action.args.recipient === "string"
          ? action.args.recipient
          : "未提供";
        const pdfPath = typeof action.args.pdf_path === "string"
          ? action.args.pdf_path
          : "/workspace/output/final_report.pdf";
        const markdownPath = typeof action.args.markdown_path === "string"
          ? action.args.markdown_path
          : "/workspace/output/final_report.md";
        const pdfFilename = pdfPath.split("/").pop() || "final_report.pdf";
        const markdownFilename = markdownPath.split("/").pop() || "final_report.md";
        const zipFilename = `${markdownFilename.replace(/\.md$/i, "")}-bundle.zip`;
        const emailSubject = typeof action.args.subject === "string" && action.args.subject.trim()
          ? action.args.subject.trim()
          : `研究报告：${pdfFilename.replace(/\.pdf$/i, "")}`;

        return (
          <div className="interrupt-card__request" key={`${action.name}-${index}`}>
            <strong>
              {isQuestion ? question : isEmail ? "是否确认发送报告邮件？" : "是否允许下载此文件？"}
            </strong>
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
                {isEmail ? (
                  <div className="interrupt-card__email-details">
                    <span><b>收件邮箱</b>{recipient}</span>
                    <span><b>邮件主题</b>{emailSubject}</span>
                    <span><b>PDF 附件</b>{pdfFilename}</span>
                    <span><b>完整 ZIP</b>{zipFilename}</span>
                  </div>
                ) : <code>{filePath}</code>}
                <div className="interrupt-card__choices">
                  {allowed.includes("approve") ? (
                    <button
                      type="button"
                      className={choices[index] === "approve" ? "is-selected" : ""}
                      onClick={() => setChoices((current) => ({ ...current, [index]: "approve" }))}
                      disabled={submitting}
                    >
                      {isEmail ? "确认发送" : "批准下载"}
                    </button>
                  ) : null}
                  {allowed.includes("reject") ? (
                    <button
                      type="button"
                      className={choices[index] === "reject" ? "is-rejected" : ""}
                      onClick={() => setChoices((current) => ({ ...current, [index]: "reject" }))}
                      disabled={submitting}
                    >
                      {isEmail ? "取消发送" : "拒绝"}
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
  const configuredApiUrl = import.meta.env.VITE_LANGGRAPH_API_URL ?? "http://127.0.0.1:2024";
  // LangGraph SDK 内部使用 new URL() 拼接请求路径，因此生产环境的 /api
  // 必须先解析为当前站点下的绝对地址。
  const apiUrl = new URL(configuredApiUrl, `${window.location.origin}/`)
    .toString()
    .replace(/\/$/, "");
  const assistantId = import.meta.env.VITE_LANGGRAPH_ASSISTANT_ID ?? "supervisor";
  const [threadId, setThreadId] = useState<string | undefined>(
    () => new URLSearchParams(window.location.search).get("thread") ?? undefined,
  );
  const [input, setInput] = useState("");
  const [composerInputHeight, setComposerInputHeight] = useState(COMPOSER_TEXTAREA_MIN_HEIGHT);
  const [authToken, setAuthToken] = useState<string | null>(
    () => window.localStorage.getItem(AUTH_TOKEN_KEY),
  );
  const [authUser, setAuthUser] = useState<AuthUser>(DEFAULT_USER);
  const [authStatus, setAuthStatus] = useState<AuthStatus>("checking");
  const [authMode, setAuthMode] = useState<AuthMode | null>(null);
  const [authUsername, setAuthUsername] = useState("");
  const [authPassword, setAuthPassword] = useState("");
  const [authConfirmation, setAuthConfirmation] = useState("");
  const [authError, setAuthError] = useState("");
  const [authSubmitting, setAuthSubmitting] = useState(false);
  const [sessions, setSessions] = useState<ConversationThread[]>([]);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionsError, setSessionsError] = useState("");
  const [limitDialog, setLimitDialog] = useState<LimitDialog | null>(null);
  const [limitCountdown, setLimitCountdown] = useState(0);
  const [deletingThreadId, setDeletingThreadId] = useState<string>();
  const [threadAllocating, setThreadAllocating] = useState(false);
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
  const [accountMenuOpen, setAccountMenuOpen] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [memoryClearConfirm, setMemoryClearConfirm] = useState(false);
  const [memoryClearing, setMemoryClearing] = useState(false);
  const [memoryStatus, setMemoryStatus] = useState<{
    tone: "success" | "error";
    message: string;
  } | null>(null);
  const [failureLessonSavingEnabled, setFailureLessonSavingEnabled] = useState<boolean | null>(null);
  const [memorySettingsLoading, setMemorySettingsLoading] = useState(false);
  const [memorySettingsUpdating, setMemorySettingsUpdating] = useState(false);
  const [memorySettingsError, setMemorySettingsError] = useState("");
  const [providerConfigured, setProviderConfigured] = useState<boolean | null>(null);
  const [providerVersion, setProviderVersion] = useState<number | null>(null);
  const [providerIdentity, setProviderIdentity] = useState<Pick<ModelProvider, "provider_name" | "provider_type"> | null>(null);
  const [providerDraft, setProviderDraft] = useState<ModelProviderDraft>(DEFAULT_PROVIDER_DRAFT);
  const [providerApiKey, setProviderApiKey] = useState("");
  const [providerKeyHint, setProviderKeyHint] = useState("");
  const [providerKeyVisible, setProviderKeyVisible] = useState(false);
  const [providerLoading, setProviderLoading] = useState(false);
  const [providerAction, setProviderAction] = useState<"saving" | "testing" | "deleting" | null>(null);
  const [providerStatus, setProviderStatus] = useState<{
    tone: "success" | "error";
    message: string;
  } | null>(null);
  const [liveWebSearches, setLiveWebSearches] = useState<Map<string, WebSearchProgressEvent>>(
    () => new Map(),
  );
  const [liveContextUsage, setLiveContextUsage] = useState<ContextUsageSnapshot | null>(null);
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
  const composerResizeRef = useRef<{
    pointerId: number;
    startY: number;
    startHeight: number;
  } | null>(null);
  const accountMenuRef = useRef<HTMLElement>(null);
  const accountMenuButtonRef = useRef<HTMLButtonElement>(null);
  const settingsCloseButtonRef = useRef<HTMLButtonElement>(null);
  const mainSnapshotRef = useRef<{
    threadId?: string;
    messages: Message[];
    values: StreamState;
  }>();
  const reconnectsInFlightRef = useRef<Set<string>>(new Set());
  const authHeaders = useMemo<Record<string, string>>(
    () => {
      const headers: Record<string, string> = {};
      if (authToken) headers.Authorization = `Bearer ${authToken}`;
      return headers;
    },
    [authToken],
  );
  const graphClient = useMemo(
    () => new Client({
      apiUrl,
      defaultHeaders: authHeaders,
      // SDK 在 SSE 建立前不会完整保留结构化错误；统一 fetch 保留状态码和 detail。
      callerOptions: { fetch: langGraphFetch, maxRetries: 0 },
    }),
    [apiUrl, authHeaders],
  );
  const runManager = useThreadRunManager(graphClient, assistantId);
  const authReady = authStatus === "ready";
  const workspaceLocked = !authReady;
  const providerReady = providerConfigured === true;
  useEffect(() => {
    // Live progress belongs to one authenticated thread and is never persisted locally.
    setLiveWebSearches(new Map());
    setLiveContextUsage(null);
  }, [authUser.id, providerVersion, threadId]);
  const expireAuthentication = useCallback(() => {
    // 保留当前页面数据，只撤销失效凭据并锁定后续服务端操作。
    window.localStorage.removeItem(AUTH_TOKEN_KEY);
    setAuthToken(null);
    setAuthUser(SIGNED_OUT_USER);
    setAuthStatus("required");
    setAuthError("登录已失效，请重新登录");
    setProviderConfigured(null);
    setProviderVersion(null);
    setProviderIdentity(null);
    setProviderApiKey("");
    setLiveContextUsage(null);
  }, []);

  const presentRunAdmissionError = useCallback((error: unknown): boolean => {
    if (!(error instanceof LangGraphApiError) || !error.detail) return false;
    if (error.detail.code === "MODEL_PROVIDER_NOT_CONFIGURED" || error.detail.code === "MODEL_PROVIDER_INVALID") {
      setProviderConfigured(false);
      setProviderIdentity(null);
      setProviderStatus({ tone: "error", message: error.detail.message });
      setSettingsOpen(true);
      return true;
    }
    const supported = new Set([
      "QUESTION_RATE_LIMITED",
      "THREAD_CONCURRENCY_LIMIT",
      "RATE_LIMIT_SERVICE_UNAVAILABLE",
      "TOKEN_BUDGET_EXHAUSTED",
      "RUN_ADMISSION_ALREADY_USED",
      "RUN_ADMISSION_MISMATCH",
    ]);
    if (!supported.has(error.detail.code)) return false;
    const retrySeconds = Math.max(0, error.detail.retry_after_seconds ?? 0);
    setLimitDialog({
      ...error.detail,
      retryUntil: retrySeconds > 0 ? Date.now() + retrySeconds * 1000 : undefined,
    });
    setLimitCountdown(retrySeconds);
    return true;
  }, []);

  const requestRunAdmission = useCallback(async (
    targetThreadId?: string,
  ): Promise<string | undefined> => {
    const submissionId = window.crypto.randomUUID();
    try {
      const response = await fetch(`${apiUrl}/run-admissions`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders },
        body: JSON.stringify({
          submission_id: submissionId,
          thread_id: targetThreadId ?? null,
        }),
      });
      if (response.status === 401) {
        expireAuthentication();
        return undefined;
      }
      if (!response.ok) {
        const body = await response.json().catch(() => null) as { detail?: LimitDetail | string } | null;
        const detail = typeof body?.detail === "object" ? body.detail : undefined;
        const message = detail?.message
          ?? (typeof body?.detail === "string" ? body.detail : "任务准入失败，请稍后重试");
        const error = new LangGraphApiError(response.status, message, detail);
        if (!presentRunAdmissionError(error)) throw error;
        return undefined;
      }
      return submissionId;
    } catch (error) {
      if (error instanceof LangGraphApiError) throw error;
      presentRunAdmissionError(new LangGraphApiError(503, "请求保护服务暂不可用，请稍后重试", {
        code: "RATE_LIMIT_SERVICE_UNAVAILABLE",
        message: "请求保护服务暂不可用，请稍后重试",
        retry_after_seconds: 0,
        active_thread_ids: [],
      }));
      return undefined;
    }
  }, [apiUrl, authHeaders, expireAuthentication, presentRunAdmissionError]);

  useEffect(() => {
    if (!limitDialog?.retryUntil) {
      setLimitCountdown(0);
      return undefined;
    }
    const updateCountdown = () => {
      setLimitCountdown(Math.max(0, Math.ceil((limitDialog.retryUntil! - Date.now()) / 1000)));
    };
    updateCountdown();
    const timer = window.setInterval(updateCountdown, 250);
    return () => window.clearInterval(timer);
  }, [limitDialog]);

  const loadSessions = useCallback(async (signal?: AbortSignal) => {
    if (!authReady) {
      setSessions([]);
      return;
    }
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
          if (response.status === 401) expireAuthentication();
          throw new Error(response.status === 401 ? "登录已失效，请重新登录" : "暂时无法读取会话记录");
        }
        const batch = await response.json() as ConversationThread[];
        if (!Array.isArray(batch)) throw new Error("会话服务返回了无效数据");
        collected.push(...batch);
        if (batch.length < limit) break;
        offset += limit;
      }
      setSessions(collected);
      runManager.syncThreadStatuses(collected);
    } catch (error) {
      if (signal?.aborted) return;
      setSessionsError(error instanceof Error ? error.message : "暂时无法读取会话记录");
    } finally {
      if (!signal?.aborted) setSessionsLoading(false);
    }
  }, [apiUrl, assistantId, authHeaders, authReady, expireAuthentication, runManager.syncThreadStatuses]);

  const loadArtifacts = useCallback(async (signal?: AbortSignal) => {
    if (!authReady || !threadId) {
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
        if (response.status === 401) expireAuthentication();
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
  }, [apiUrl, authHeaders, authReady, expireAuthentication, threadId]);

  const loadUploadedFiles = useCallback(async (signal?: AbortSignal) => {
    if (!authReady || !threadId) {
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
        if (response.status === 401) expireAuthentication();
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
  }, [apiUrl, authHeaders, authReady, expireAuthentication, threadId]);

  const handleCustomEvent = useCallback((value: unknown) => {
    const searchEvent = normalizeWebSearchEvent(value);
    if (searchEvent) {
      setLiveWebSearches((current) => {
        const previous = current.get(searchEvent.item_id);
        if (previous && previous.sequence_number >= searchEvent.sequence_number) return current;
        const next = new Map(current);
        next.set(searchEvent.item_id, searchEvent);
        return next;
      });
      return;
    }
    const contextEvent = normalizeContextUsageEvent(value);
    if (!contextEvent || contextEvent.provider_version !== providerVersion) return;
    setLiveContextUsage({
      used_tokens: contextEvent.used_tokens,
      max_input_tokens: contextEvent.max_input_tokens,
      provider_version: contextEvent.provider_version,
    });
  }, [providerVersion]);

  // 当前前端只持有远程图的状态类型，无法把 Python DeepAgent 类型直接传给
  // useStream；使用宽化后的选项仍可启用 SDK 内置的子智能体跟踪能力。
  const streamOptions = {
    client: graphClient,
    assistantId,
    threadId: authReady ? threadId : undefined,
    // SDK 的数字 throttle 实际采用尾部防抖；连续 token 会不断重置计时器，
    // 导致整段回答结束后才刷新。关闭它以保留逐 token 的流式显示。
    throttle: false,
    reconnectOnMount: false,
    // 子智能体消息保留在独立流中，避免混入 Supervisor 主对话。
    filterSubagentMessages: true,
    defaultHeaders: authHeaders,
    // streamSubgraphs 也会传回内部中间件子图状态。声明一个接收 state 的
    // onFinish 会让 SDK 在运行结束后重新读取主图 thread head，防止子图
    // values 成为最后一个本地快照时把主对话清空。
    onCreated: runManager.recordCreated,
    onCustomEvent: handleCustomEvent,
    onError: (error: unknown, run: { run_id: string; thread_id: string } | undefined) => {
      setThreadAllocating(false);
      setLiveWebSearches(new Map());
      // Context usage describes the persisted conversation, not one run's
      // temporary progress. Keep the last event until identity or thread changes.
      runManager.recordError(error, run);
    },
    onFinish: (state: unknown, run: { run_id: string; thread_id: string } | undefined) => {
      setThreadAllocating(false);
      setLiveWebSearches(new Map());
      const finalUsage = normalizeContextUsageSnapshot(
        typeof state === "object" && state !== null
          ? (state as StreamState).context_usage
          : null,
      );
      // Avoid a live-event → stale checkpoint handoff that makes the ring
      // briefly shrink. The final graph state is authoritative when available.
      if (finalUsage?.provider_version === providerVersion) {
        setLiveContextUsage(finalUsage);
      }
      runManager.recordFinished(run);
    },
    onThreadId: (id: string) => {
      if (!authReady) return;
      setThreadAllocating(false);
      setThreadId(id);
      const url = new URL(window.location.href);
      url.searchParams.set("thread", id);
      window.history.replaceState({}, "", url);
      // useStream 已经在服务端创建了 thread；立即刷新左栏，避免长任务必须
      // 等到整个 run 结束后才出现在会话记录中。
      void loadSessions();
    },
  };
  const baseStream = useStream<StreamState, {
    InterruptType: HITLRequest;
    CustomEventType: AppCustomEvent;
  }>(streamOptions);
  const stream = baseStream as typeof baseStream & {
    subagents: Map<string, SubagentTraceStream>;
  };
  useEffect(() => {
    if (!authToken || !stream.error || typeof stream.error !== "object") return;
    const error = stream.error as {
      status?: unknown;
      statusCode?: unknown;
      response?: { status?: unknown };
    };
    if (error.status === 401 || error.statusCode === 401 || error.response?.status === 401) {
      expireAuthentication();
    }
  }, [authToken, expireAuthentication, stream.error]);
  const joinStreamRef = useRef(stream.joinStream);
  const streamLoadingRef = useRef(stream.isLoading);
  joinStreamRef.current = stream.joinStream;
  streamLoadingRef.current = stream.isLoading;
  const currentSession = useMemo(
    () => sessions.find((session) => session.thread_id === threadId),
    [sessions, threadId],
  );

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
  const persistedContextUsage = normalizeContextUsageSnapshot(displayedValues?.context_usage);
  const contextUsage = liveContextUsage?.provider_version === providerVersion
    ? liveContextUsage
    : persistedContextUsage?.provider_version === providerVersion
      ? persistedContextUsage
      : null;

  useLayoutEffect(() => {
    if (liveMessages.length === 0) return;
    // 只缓存含主对话消息的快照。内部中间件子图没有 messages，不能覆盖它。
    mainSnapshotRef.current = {
      threadId,
      messages: liveMessages,
      values: liveValues,
    };
  }, [liveMessages, liveValues, threadId]);

  // 并发同步子图结束时，SDK 偶尔不会把父图 task ToolMessage 归并为
  // subagent complete。主会话中的同 tool_call_id 结果是最终事实来源。
  const displayedSubagents = useMemo(
    () => reconcileSubagentStatuses(stream.subagents, displayedMessages),
    [displayedMessages, stream.subagents],
  );

  // 轻量模式不构造工具卡和完整轨迹，只整理每轮最终回复与当前活动状态。
  const rows = useMemo(
    () => showDetails ? buildRows(displayedMessages) : EMPTY_ROWS,
    [displayedMessages, showDetails],
  );
  const visibleRows = useMemo(
    () => rows.slice(Math.max(0, rows.length - visibleRowLimit)),
    [rows, visibleRowLimit],
  );
  const hiddenRowCount = rows.length - visibleRows.length;
  const persistedSearchIds = useMemo(
    () => new Set(rows.flatMap((row) => row.kind === "web_search" ? [row.search.item_id] : [])),
    [rows],
  );
  const visibleLiveWebSearches = useMemo(
    () => [...liveWebSearches.values()]
      .filter((search) => !persistedSearchIds.has(search.item_id))
      .sort((left, right) => left.output_index - right.output_index),
    [liveWebSearches, persistedSearchIds],
  );
  const latestLiveWebSearch = visibleLiveWebSearches.at(-1);
  const lastMessageKey = useMemo(() => {
    for (let index = rows.length - 1; index >= 0; index -= 1) {
      const row = rows[index];
      if (row?.kind === "message") return row.key;
    }
    return undefined;
  }, [rows]);
  const todos = Array.isArray(displayedValues?.todos) ? displayedValues.todos : [];
  const pendingInterrupt = useMemo(
    () => hitlRequest(stream.interrupt?.value),
    [stream.interrupt?.value],
  );
  const currentRuntime = threadId ? runManager.runtimes[threadId] : undefined;
  const queuedEntries = currentRuntime?.queuedRuns ?? [];
  const currentQueueSize = queuedEntries.length;
  const compactTurns = useMemo(() => {
    if (showDetails) return [];
    const queuedBodies = queuedEntries.map((entry) => queuedMessageText(entry.values));
    const baseTurns = withoutQueuedTurns(buildCompactTurns(displayedMessages), queuedBodies);
    const activeIndex = stream.isLoading ? baseTurns.length - 1 : -1;
    return baseTurns.map((turn, index): CompactTurn => ({
      ...turn,
      activity: compactActivity(turn, {
        active: index === activeIndex,
        latest: index === baseTurns.length - 1,
        todos,
        interrupted: pendingInterrupt !== null,
        failed: Boolean(stream.error),
        webSearch: latestLiveWebSearch,
      }),
    }));
  }, [displayedMessages, latestLiveWebSearch, pendingInterrupt, queuedEntries, showDetails, stream.error, stream.isLoading, todos]);
  const activeCompactTurn = stream.isLoading ? compactTurns.at(-1) : undefined;
  const compactFollowKey = activeCompactTurn
    ? `${activeCompactTurn.userKey}:${activeCompactTurn.activity.kind}:${activeCompactTurn.activity.text}`
    : `${compactTurns.length}:${compactTurns.at(-1)?.assistantKey ?? ""}`;
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
    if (!threadId) return;
    void runManager.cancelActive(threadId).then(() => loadSessions()).catch((error: unknown) => {
      setSessionsError(error instanceof Error ? `停止回答失败：${error.message}` : "停止回答失败");
    });
  }, [loadSessions, runManager.cancelActive, threadId]);
  const runningTaskCount = tasks.filter(
    (task) => task.status === "running" || task.status === "pending",
  ).length;
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
    || currentQueueSize > 0
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
    if (!authReady || !threadId || taskPollInFlightRef.current) return;
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
        if (response.status === 401) expireAuthentication();
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
  }, [apiUrl, authHeaders, authReady, expireAuthentication, threadId]);

  const downloadArtifact = useCallback(async (
    artifact: DownloadableArtifact,
    mode: "auto" | "raw" | "bundle" = "auto",
  ) => {
    if (!authReady) throw new Error("请先登录");
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
        if (response.status === 401) expireAuthentication();
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
  }, [apiUrl, authHeaders, authReady, expireAuthentication, threadId]);

  useEffect(() => {
    const controller = new AbortController();
    void loadSessions(controller.signal);
    return () => controller.abort();
  }, [loadSessions]);

  useEffect(() => {
    if (!authReady) runManager.reset();
  }, [authReady, runManager.reset]);

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
    if (!authReady || !threadId || stream.isLoading || currentSession?.status !== "busy") return undefined;

    const controller = new AbortController();
    // 服务端 run 是最终事实来源；只对当前可见 thread 建立一条流连接。
    const timerId = window.setTimeout(() => {
      if (streamLoadingRef.current) return;
      void runManager.reconcile(threadId, controller.signal).then(async ({ running, pending }) => {
        if (controller.signal.aborted || streamLoadingRef.current) return;
        const activeRun = running ?? pending[0];
        if (!activeRun) return;

        const reconnectKey = `${threadId}:${activeRun.run_id}`;
        if (reconnectsInFlightRef.current.has(reconnectKey)) return;
        reconnectsInFlightRef.current.add(reconnectKey);
        runManager.markConnecting(threadId, activeRun.run_id);
        try {
          runManager.markConnected(threadId, activeRun.run_id);
          await joinStreamRef.current(activeRun.run_id);
        } catch (error) {
          if (!controller.signal.aborted) {
            runManager.recordError(error, {
              thread_id: threadId,
              run_id: activeRun.run_id,
            });
            setSessionsError(
              error instanceof Error
                ? `运行仍在服务端继续，但实时流重连失败：${error.message}`
              : "运行仍在服务端继续，但实时流重连失败",
            );
          }
        } finally {
          reconnectsInFlightRef.current.delete(reconnectKey);
        }
      }).catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setSessionsError(
          error instanceof Error
            ? `无法查询当前运行：${error.message}`
            : "无法查询当前运行",
        );
      });
    }, 300);

    return () => {
      window.clearTimeout(timerId);
      controller.abort();
    };
  }, [
    authReady,
    currentSession?.status,
    runManager.markConnected,
    runManager.markConnecting,
    runManager.reconcile,
    runManager.recordError,
    stream.isLoading,
    threadId,
  ]);

  useEffect(() => {
    const hasBusySession = sessions.some((session) => session.status === "busy");
    if (!authReady || (!stream.isLoading && !hasBusySession)) {
      return undefined;
    }
    // 任意后台会话忙碌时都保持左栏状态新鲜，当前会话的完成刷新仍由
    // isLoading 下降沿负责。
    const intervalId = window.setInterval(() => void loadSessions(), 5_000);
    return () => window.clearInterval(intervalId);
  }, [authReady, currentSession?.status, loadSessions, sessions, stream.isLoading, threadId]);

  useEffect(() => {
    const controller = new AbortController();
    setAuthStatus("checking");
    void (async () => {
      try {
        const response = await fetch(`${apiUrl}/auth/me`, {
          headers: authHeaders,
          signal: controller.signal,
        });
        if (response.status === 401) {
          const hadToken = Boolean(authToken);
          window.localStorage.removeItem(AUTH_TOKEN_KEY);
          if (hadToken) setAuthToken(null);
          setAuthUser(SIGNED_OUT_USER);
          setAuthStatus("required");
          setAuthError(hadToken ? "登录已失效，请重新登录" : "");
          return;
        }
        if (!response.ok) throw new Error("账户服务不可用");
        const { user } = await response.json() as { user?: AuthUser };
        if (!user?.id || !user.username) throw new Error("账户服务返回了无效数据");
        setAuthUser(user);
        setAuthStatus("ready");
        setAuthError("");
      } catch (error) {
        if (controller.signal.aborted) return;
        setAuthUser(SIGNED_OUT_USER);
        setAuthStatus("unavailable");
        setAuthError(error instanceof Error ? error.message : "账户服务不可用");
      }
    })();
    return () => controller.abort();
  }, [apiUrl, authHeaders, authToken]);

  useEffect(() => {
    if (!authReady) {
      setFailureLessonSavingEnabled(null);
      setMemorySettingsLoading(false);
      setMemorySettingsError("");
      return undefined;
    }
    const controller = new AbortController();
    setMemorySettingsLoading(true);
    setMemorySettingsError("");
    void (async () => {
      try {
        const response = await fetch(`${apiUrl}/memories/settings`, {
          headers: authHeaders,
          signal: controller.signal,
        });
        const body = await response.json() as MemorySettingsResponse;
        if (response.status === 401) {
          expireAuthentication();
          return;
        }
        if (!response.ok || typeof body.failure_lesson_saving_enabled !== "boolean") {
          throw new Error(body.detail || "无法读取失败经验设置");
        }
        setFailureLessonSavingEnabled(body.failure_lesson_saving_enabled);
      } catch (error) {
        if (controller.signal.aborted) return;
        setFailureLessonSavingEnabled(null);
        setMemorySettingsError(error instanceof Error ? error.message : "无法读取失败经验设置");
      } finally {
        if (!controller.signal.aborted) setMemorySettingsLoading(false);
      }
    })();
    return () => controller.abort();
  }, [apiUrl, authHeaders, authReady, authUser.id, expireAuthentication]);

  useEffect(() => {
    if (!authReady) {
      setProviderConfigured(null);
      setProviderVersion(null);
      setProviderIdentity(null);
      setProviderApiKey("");
      setProviderKeyHint("");
      setProviderLoading(false);
      setProviderStatus(null);
      return undefined;
    }
    const controller = new AbortController();
    setProviderLoading(true);
    void (async () => {
      try {
        const response = await fetch(`${apiUrl}/model-provider`, {
          headers: authHeaders,
          signal: controller.signal,
        });
        const body = await response.json() as ModelProviderResponse;
        if (response.status === 401) {
          expireAuthentication();
          return;
        }
        if (!response.ok) throw new Error("无法读取模型 Provider 配置");
        const provider = body.provider;
        setProviderConfigured(body.configured === true && Boolean(provider));
        setProviderVersion(provider?.version ?? null);
        setProviderIdentity(provider ? {
          provider_name: provider.provider_name,
          provider_type: provider.provider_type,
        } : null);
        setProviderApiKey("");
        setProviderKeyHint(provider?.api_key_hint ?? "");
        if (provider) {
          setProviderDraft({
            base_url: provider.base_url,
            model_name: provider.model_name,
          });
        } else {
          setProviderDraft(DEFAULT_PROVIDER_DRAFT);
        }
      } catch (error) {
        if (controller.signal.aborted) return;
        setProviderConfigured(false);
        setProviderVersion(null);
        setProviderIdentity(null);
        setProviderStatus({
          tone: "error",
          message: error instanceof Error ? error.message : "无法读取模型 Provider 配置",
        });
      } finally {
        if (!controller.signal.aborted) setProviderLoading(false);
      }
    })();
    return () => controller.abort();
  }, [apiUrl, authHeaders, authReady, authUser.id, expireAuthentication]);

  useEffect(() => {
    if (!accountMenuOpen) return undefined;
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (!accountMenuRef.current?.contains(event.target as Node)) {
        setAccountMenuOpen(false);
      }
    };
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        setAccountMenuOpen(false);
        accountMenuButtonRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", closeOnOutsideClick);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeOnOutsideClick);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [accountMenuOpen]);

  useEffect(() => {
    if (!settingsOpen) return undefined;
    settingsCloseButtonRef.current?.focus();
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape" || memoryClearing || memorySettingsUpdating || providerAction) return;
      setSettingsOpen(false);
      setMemoryClearConfirm(false);
      accountMenuButtonRef.current?.focus();
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [memoryClearing, memorySettingsUpdating, providerAction, settingsOpen]);

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
    if (!authReady || !threadId || !pollingTaskKey) return undefined;
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
  }, [authReady, pollingTaskKey, refreshTaskStatuses, threadId]);

  useEffect(() => {
    if (!authReady || stream.isLoading || currentQueueSize > 0) return;
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
    void (async () => {
      const submissionId = await requestRunAdmission(threadId);
      if (!submissionId) {
        runKeys.forEach((key) => autoCollectedTaskRunsRef.current.delete(key));
        return;
      }
      try {
        await stream.submit(
          {
            messages: [{
              type: "human",
              name: "async-task-monitor",
              content: `后台任务 ${taskIds} 已完成。请调用 check_async_task 读取结果并继续处理，不要重新启动任务。`,
            }],
          },
          {
            metadata: { deep_data_ui: { submission_id: submissionId } },
            streamSubgraphs: true,
            streamResumable: true,
            onDisconnect: "continue",
          },
        );
      } catch (error) {
        runKeys.forEach((key) => autoCollectedTaskRunsRef.current.delete(key));
        if (!presentRunAdmissionError(error)) {
          setTaskRefreshError("任务已完成，但自动读取结果失败，请点击“读取结果”重试");
        }
      }
    })();
  }, [authReady, currentQueueSize, presentRunAdmissionError, requestRunAdmission, stream.isLoading, stream.submit, tasks, threadId, trackedTasks]);

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
  }, [compactFollowKey, rows, showDetails, stream.isLoading]);

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
    if (!authReady) throw new Error("请先登录");
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
      if (response.status === 401) expireAuthentication();
      throw new Error(response.status === 401 ? "登录已失效，请重新登录" : "创建文件分析会话失败");
    }
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
      if (response.status === 401) expireAuthentication();
      throw new Error(payload.detail || "文件上传失败，请稍后重试");
    }
    setUploadedFiles((current) => current.map((file) => (
      file.key === item.key
        ? { ...uploaded, key: item.key, status: "ready" as const }
        : file
    )));
  }

  async function onFilesSelected(event: ChangeEvent<HTMLInputElement>) {
    if (!authReady) return;
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
      if (!response.ok) {
        if (response.status === 401) expireAuthentication();
        throw new Error(payload.detail || "删除上传文件失败");
      }
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

  async function submitText(text: string, mode: SubmitMode = "enqueue") {
    const value = text.trim();
    if (!authReady || !value || !filesReadyForAnalysis || threadAllocating) return;
    if (!providerReady) {
      setProviderStatus({ tone: "error", message: "请先配置模型 Provider" });
      setSettingsOpen(true);
      return;
    }
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
    if (!threadId) setThreadAllocating(true);
    let submissionId: string | undefined;
    try {
      submissionId = await requestRunAdmission(threadId);
    } catch (error) {
      setThreadAllocating(false);
      setSessionsError(error instanceof Error ? error.message : "任务准入失败，请稍后重试");
      return;
    }
    if (!submissionId) {
      setThreadAllocating(false);
      return;
    }
    setInput("");
    if (stream.isLoading && mode === "enqueue") {
      if (!threadId) {
        setInput(value);
        setThreadAllocating(false);
        return;
      }
      void runManager.enqueue(
        threadId,
        { messages: [{ type: "human", content: message }] },
        message,
        submissionId,
      ).catch((error: unknown) => {
        setInput((current) => current || value);
        if (!presentRunAdmissionError(error)) {
          setSessionsError(error instanceof Error ? `消息排队失败：${error.message}` : "消息排队失败");
        }
      });
      return;
    }
    const multitaskOptions = stream.isLoading ? { multitaskStrategy: mode } : {};

    setLiveWebSearches(new Map());
    void stream.submit(
      { messages: [{ type: "human", content: message }] },
      {
        ...multitaskOptions,
        // LangGraph 会在首个图节点完成后才返回主图 values。先本地插入用户消息，
        // 避免新会话在模型思考或进入子图期间重新显示空首页。
        optimisticValues: (current) => ({
          messages: [...(current.messages ?? []), optimisticMessage],
        }),
        metadata: {
          deep_data_ui: { submission_id: submissionId },
          ...(!threadId && stream.messages.length === 0 ? {
            kind: "conversation",
            title: conversationTitle(value),
          } : {}),
        },
        streamResumable: true,
        streamSubgraphs: true,
        onDisconnect: "continue",
      },
    ).catch((error: unknown) => {
      setThreadAllocating(false);
      setInput((current) => current || value);
      if (!presentRunAdmissionError(error)) {
        setSessionsError(error instanceof Error ? `发送任务失败：${error.message}` : "发送任务失败");
      }
    });
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
      const submissionId = await requestRunAdmission(threadId);
      if (!submissionId) return;
      setLiveWebSearches(new Map());
      await stream.submit(null, {
        command: { resume: { decisions } },
        metadata: { deep_data_ui: { submission_id: submissionId } },
        streamResumable: true,
        streamSubgraphs: true,
        onDisconnect: "continue",
      });
    } catch (error) {
      approvedSemanticDownloadRef.current = false;
      semanticDownloadBaselineRef.current = null;
      if (!presentRunAdmissionError(error)) {
        setInterruptError(error instanceof Error ? error.message : "暂时无法恢复任务");
      }
    } finally {
      setInterruptSubmitting(false);
    }
  }

  function onSubmit(event: FormEvent) {
    event.preventDefault();
    void submitText(input);
  }

  function onComposerKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submitText(input);
    }
  }

  function checkTask(taskId: string) {
    void submitText(
      `请调用 check_async_task 检查任务 ${taskId}。如果远程运行已经结束，请读取结果第一行的业务 status 并继续处理。`,
    );
  }

  function updateTask(taskId: string, message: string) {
    void submitText(
      `请调用 update_async_task 更新任务 ${taskId}。补充要求：${message}`,
      "interrupt",
    );
  }

  function cancelTask(taskId: string) {
    const confirmed = window.confirm(
      "确定取消这个后台任务吗？已经完成的请求和文件不会自动删除。",
    );
    if (!confirmed) return;
    void submitText(`请调用 cancel_async_task 取消任务 ${taskId}。`, "interrupt");
  }

  function refreshTasks() {
    void refreshTaskStatuses(undefined, true);
  }

  function composerMaxHeight() {
    return Math.max(COMPOSER_TEXTAREA_MIN_HEIGHT, Math.min(420, window.innerHeight * 0.5));
  }

  function startComposerResize(event: ReactPointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    composerResizeRef.current = {
      pointerId: event.pointerId,
      startY: event.clientY,
      startHeight: composerInputHeight,
    };
  }

  function moveComposerResize(event: ReactPointerEvent<HTMLDivElement>) {
    const resize = composerResizeRef.current;
    if (!resize || resize.pointerId !== event.pointerId) return;
    const nextHeight = resize.startHeight + resize.startY - event.clientY;
    setComposerInputHeight(Math.round(Math.max(
      COMPOSER_TEXTAREA_MIN_HEIGHT,
      Math.min(composerMaxHeight(), nextHeight),
    )));
  }

  function finishComposerResize(event: ReactPointerEvent<HTMLDivElement>) {
    if (composerResizeRef.current?.pointerId !== event.pointerId) return;
    composerResizeRef.current = null;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
  }

  function resizeComposerWithKeyboard(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
    event.preventDefault();
    const direction = event.key === "ArrowUp" ? 24 : -24;
    setComposerInputHeight((current) => Math.max(
      COMPOSER_TEXTAREA_MIN_HEIGHT,
      Math.min(composerMaxHeight(), current + direction),
    ));
  }

  function clearQueuedMessages() {
    if (!threadId) return;
    void runManager.clearPending(threadId).catch((error: unknown) => {
      setSessionsError(error instanceof Error ? `清空排队消息失败：${error.message}` : "清空排队消息失败");
    });
  }

  function cancelQueuedMessage(runId: string) {
    if (!threadId) return;
    void runManager.cancelPending(threadId, runId).catch((error: unknown) => {
      setSessionsError(error instanceof Error ? `取消排队消息失败：${error.message}` : "取消排队消息失败");
    });
  }

  function jumpToBottom() {
    autoFollowRef.current = true;
    setShowJumpToBottom(false);
    scrollViewportToBottom();
  }

  function startNewThread() {
    if (!authReady || threadAllocating || filesUploading || interruptSubmitting) return;
    if (threadId) runManager.markDetached(threadId);
    setThreadAllocating(false);
    setThreadId(undefined);
    setInput("");
    setUploadedFiles([]);
    setFileError("");
    window.history.replaceState({}, "", window.location.pathname);
  }

  function selectSession(nextThreadId: string) {
    if (!authReady || nextThreadId === threadId || threadAllocating || filesUploading || interruptSubmitting) return;
    if (threadId) runManager.markDetached(threadId);
    setThreadId(nextThreadId);
    setInput("");
    setUploadedFiles([]);
    const url = new URL(window.location.href);
    url.searchParams.set("thread", nextThreadId);
    window.history.replaceState({}, "", url);
  }

  async function deleteSession(targetThreadId: string) {
    if (!authReady || deletingThreadId || filesUploading || interruptSubmitting) return;
    setDeletingThreadId(targetThreadId);
    setSessionsError("");
    try {
      const active = await runManager.reconcile(targetThreadId);
      if (active.running || active.pending.length > 0) {
        const confirmed = window.confirm(
          "该会话仍有运行中或排队任务。删除将先取消这些任务，确定继续吗？",
        );
        if (!confirmed) return;
        await runManager.cancelAllAndWait(targetThreadId);
      }
      const response = await fetch(`${apiUrl}/threads/${encodeURIComponent(targetThreadId)}`, {
        method: "DELETE",
        headers: authHeaders,
      });
      if (!response.ok) {
        if (response.status === 401) expireAuthentication();
        throw new Error(response.status === 401 ? "登录已失效，请重新登录" : "删除会话失败，请稍后重试");
      }
      setSessions((current) => current.filter((session) => session.thread_id !== targetThreadId));
      runManager.removeThread(targetThreadId);
      if (targetThreadId === threadId) {
        // 删除当前会话后直接进入空白会话，不保留已经删除的 URL。
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
    if (threadId) runManager.markDetached(threadId);
    runManager.reset();
    setThreadId(undefined);
    setInput("");
    setUploadedFiles([]);
    setFileError("");
    window.history.replaceState({}, "", window.location.pathname);
  }

  function openAuth(mode: AuthMode) {
    // 认证失效时不能要求用户先操作已经无权访问的旧运行。
    if (!workspaceLocked && identitySwitchBlocked) return;
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
      setAuthStatus("ready");
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
      if (!response.ok) throw new Error("退出登录失败，请稍后重试");
      window.localStorage.removeItem(AUTH_TOKEN_KEY);
      setAuthToken(null);
      setAuthStatus("checking");
      resetThreadForIdentityChange();
    } catch (error) {
      setAuthError(error instanceof Error ? error.message : "账户服务不可用");
    }
  }

  function openSettings() {
    setAccountMenuOpen(false);
    setMemoryClearConfirm(false);
    setMemoryStatus(null);
    setSettingsOpen(true);
  }

  function closeSettings() {
    if (memoryClearing || memorySettingsUpdating || providerAction) return;
    setSettingsOpen(false);
    setMemoryClearConfirm(false);
    accountMenuButtonRef.current?.focus();
  }

  function providerRequestBody() {
    return {
      ...providerDraft,
      ...(providerApiKey.trim() ? { api_key: providerApiKey } : {}),
    };
  }

  async function testModelProvider() {
    if (!authReady || providerAction || providerLoading) return;
    if (!providerConfigured && !providerApiKey.trim()) {
      setProviderStatus({ tone: "error", message: "首次测试前请填写 API Key" });
      return;
    }
    setProviderAction("testing");
    setProviderStatus(null);
    try {
      const response = await fetch(`${apiUrl}/model-provider/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json", ...authHeaders },
        body: JSON.stringify(providerRequestBody()),
      });
      const body = await response.json() as ModelProviderResponse & { latency_ms?: number };
      if (response.status === 401) {
        expireAuthentication();
        setSettingsOpen(false);
        return;
      }
      if (!response.ok) throw new Error(providerErrorMessage(body, "模型 Provider 连接失败"));
      if (body.provider_name && body.provider_type) {
        setProviderIdentity({
          provider_name: body.provider_name,
          provider_type: body.provider_type,
        });
      }
      setProviderStatus({
        tone: "success",
        message: `连接成功${body.provider_type ? ` · ${PROVIDER_TYPE_LABELS[body.provider_type]}` : ""}${typeof body.latency_ms === "number" ? ` · ${body.latency_ms} ms` : ""}`,
      });
    } catch (error) {
      setProviderStatus({
        tone: "error",
        message: error instanceof Error ? error.message : "模型 Provider 连接失败",
      });
    } finally {
      setProviderAction(null);
    }
  }

  async function saveModelProvider() {
    if (!authReady || providerAction || providerLoading || identitySwitchBlocked) return;
    if (!providerConfigured && !providerApiKey.trim()) {
      setProviderStatus({ tone: "error", message: "首次保存时必须填写 API Key" });
      return;
    }
    setProviderAction("saving");
    setProviderStatus(null);
    try {
      const response = await fetch(`${apiUrl}/model-provider`, {
        method: "PUT",
        headers: { "Content-Type": "application/json", ...authHeaders },
        body: JSON.stringify(providerRequestBody()),
      });
      const body = await response.json() as ModelProviderResponse;
      if (response.status === 401) {
        expireAuthentication();
        setSettingsOpen(false);
        return;
      }
      if (!response.ok || !body.provider) {
        throw new Error(providerErrorMessage(body, "保存模型 Provider 失败"));
      }
      setProviderConfigured(true);
      setProviderVersion(body.provider.version);
      setProviderIdentity({
        provider_name: body.provider.provider_name,
        provider_type: body.provider.provider_type,
      });
      setProviderDraft({
        base_url: body.provider.base_url,
        model_name: body.provider.model_name,
      });
      setProviderKeyHint(body.provider.api_key_hint);
      setProviderApiKey("");
      setProviderStatus({ tone: "success", message: "模型 Provider 已保存" });
    } catch (error) {
      setProviderStatus({
        tone: "error",
        message: error instanceof Error ? error.message : "保存模型 Provider 失败",
      });
    } finally {
      setProviderAction(null);
    }
  }

  async function deleteModelProvider() {
    if (!authReady || !providerConfigured || providerAction || identitySwitchBlocked) return;
    if (!window.confirm("删除当前模型 Provider？删除后将无法发起新任务。")) return;
    setProviderAction("deleting");
    setProviderStatus(null);
    try {
      const response = await fetch(`${apiUrl}/model-provider`, {
        method: "DELETE",
        headers: authHeaders,
      });
      const body = await response.json() as ModelProviderResponse;
      if (response.status === 401) {
        expireAuthentication();
        setSettingsOpen(false);
        return;
      }
      if (!response.ok) throw new Error(providerErrorMessage(body, "删除模型 Provider 失败"));
      setProviderConfigured(false);
      setProviderVersion(null);
      setProviderIdentity(null);
      setProviderDraft(DEFAULT_PROVIDER_DRAFT);
      setProviderApiKey("");
      setProviderKeyHint("");
      setProviderStatus({ tone: "success", message: "模型 Provider 已删除" });
    } catch (error) {
      setProviderStatus({
        tone: "error",
        message: error instanceof Error ? error.message : "删除模型 Provider 失败",
      });
    } finally {
      setProviderAction(null);
    }
  }

  async function clearUserMemory() {
    if (!authReady || identitySwitchBlocked || memoryClearing) return;
    setMemoryClearing(true);
    setMemoryStatus(null);
    try {
      const response = await fetch(`${apiUrl}/memories/user`, {
        method: "DELETE",
        headers: authHeaders,
      });
      const body = await response.json() as MemoryClearResponse;
      if (response.status === 401) {
        expireAuthentication();
        setSettingsOpen(false);
        return;
      }
      if (!response.ok || body.status !== "cleared") {
        throw new Error(body.detail || "清除记忆失败，请稍后重试");
      }
      setMemoryClearConfirm(false);
      setMemoryStatus({
        tone: "success",
        message: "记忆已清除，将从下一次任务起生效。",
      });
    } catch (error) {
      setMemoryStatus({
        tone: "error",
        message: error instanceof Error ? error.message : "记忆服务暂不可用，请稍后重试",
      });
    } finally {
      setMemoryClearing(false);
    }
  }

  async function updateFailureLessonSaving() {
    if (!authReady || failureLessonSavingEnabled === null || memorySettingsUpdating) return;
    const nextEnabled = !failureLessonSavingEnabled;
    setMemorySettingsUpdating(true);
    setMemorySettingsError("");
    try {
      const response = await fetch(`${apiUrl}/memories/settings`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json", ...authHeaders },
        body: JSON.stringify({ failure_lesson_saving_enabled: nextEnabled }),
      });
      const body = await response.json() as MemorySettingsResponse;
      if (response.status === 401) {
        expireAuthentication();
        setSettingsOpen(false);
        return;
      }
      if (!response.ok || typeof body.failure_lesson_saving_enabled !== "boolean") {
        throw new Error(body.detail || "更新失败经验设置失败");
      }
      setFailureLessonSavingEnabled(body.failure_lesson_saving_enabled);
    } catch (error) {
      setMemorySettingsError(error instanceof Error ? error.message : "更新失败经验设置失败");
    } finally {
      setMemorySettingsUpdating(false);
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
            <span>{workspaceLocked ? "登录后启用 Supervisor" : !providerReady ? "请先配置模型 Provider" : stream.isLoading ? "Supervisor 正在回答" : "Supervisor 入口就绪"}</span>
          </div>
          <code title={threadId ?? "等待创建"}>{workspaceLocked ? "会话功能已锁定" : threadId ?? "新会话 · 等待首次消息"}</code>
          <button
            type="button"
            disabled={workspaceLocked || threadAllocating || filesUploading || interruptSubmitting}
            onClick={startNewThread}
          >
            开始新任务
          </button>
        </div>

        <SessionHistory
          sessions={sessions}
          runtimes={runManager.runtimes}
          currentThreadId={threadId}
          loading={sessionsLoading}
          error={sessionsError}
          switchingDisabled={workspaceLocked || threadAllocating || filesUploading || interruptSubmitting}
          deletingThreadId={deletingThreadId}
          deleteCurrentDisabled={workspaceLocked || threadAllocating || filesUploading || interruptSubmitting}
          onSelect={selectSession}
          onDelete={(targetThreadId) => void deleteSession(targetThreadId)}
          onRefresh={() => void loadSessions()}
        />

        <section className="account-card" aria-label="当前账户" ref={accountMenuRef}>
          <div className="account-card__identity">
            <span aria-hidden="true">{workspaceLocked ? "访" : authUser.is_default ? "访" : authUser.username.slice(0, 1).toUpperCase()}</span>
            <div>
              <small>{authStatus === "checking" ? "正在确认身份" : workspaceLocked ? "需要认证" : authUser.is_default ? "共享身份" : "个人空间"}</small>
              <strong>{workspaceLocked ? SIGNED_OUT_USER.username : authUser.username}</strong>
            </div>
            <button
              ref={accountMenuButtonRef}
              className="account-card__menu-trigger"
              type="button"
              aria-label="打开账户菜单"
              aria-haspopup="menu"
              aria-expanded={accountMenuOpen}
              onClick={() => setAccountMenuOpen((current) => !current)}
            >
              <span aria-hidden="true">•••</span>
            </button>
          </div>
          {accountMenuOpen ? (
            <div className="account-menu" role="menu" aria-label="账户菜单">
              <button type="button" role="menuitem" onClick={openSettings}>
                <span aria-hidden="true">⚙</span>
                设置
              </button>
              {authReady && !authUser.is_default ? (
                <button
                  type="button"
                  role="menuitem"
                  disabled={identitySwitchBlocked}
                  onClick={() => {
                    setAccountMenuOpen(false);
                    void logout();
                  }}
                >
                  <span aria-hidden="true">↪</span>
                  退出登录
                </button>
              ) : null}
            </div>
          ) : null}
          {workspaceLocked || authUser.is_default ? (
            <div className="account-card__actions">
                <button type="button" disabled={!workspaceLocked && identitySwitchBlocked} onClick={() => openAuth("login")}>登录</button>
                <button type="button" disabled={!workspaceLocked && identitySwitchBlocked} onClick={() => openAuth("register")}>注册</button>
            </div>
          ) : null}
          {!workspaceLocked && identitySwitchBlocked ? <p>结束当前运行和后台任务后可切换账户。</p> : null}
          {authStatus === "required" ? <p>请登录或注册后开始研究任务。</p> : null}
          {!authMode && authError ? <p className="account-card__error">{authError}</p> : null}
        </section>

        <div className="sidebar-foot">
          <span>Agent API</span>
          <code>{apiUrl.replace(/^https?:\/\//, "")}</code>
        </div>
      </aside>

      <main
        className="main-panel"
        style={{ "--composer-input-height": `${composerInputHeight}px` } as CSSProperties}
      >
        <header className="topbar">
          <div>
            <p className="eyebrow">DeepAgents · Tavily</p>
            <h1>从网页与文件到可追溯结论</h1>
          </div>
          <div className="topbar__status">
            <div className="runtime-stats" aria-label="运行状态">
              <span>Supervisor：{stream.isLoading ? "回答中" : "空闲"}</span>
              <span>后台任务：{runningTaskCount}</span>
              <span>等待处理：{currentQueueSize}</span>
            </div>
            <i className={stream.isLoading ? "is-active" : ""} aria-hidden="true" />
          </div>
        </header>

        <section ref={conversationRef} className="conversation" aria-label="研究对话">
          {(showDetails ? rows.length === 0 : compactTurns.length === 0) ? (
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
                subagent={row.card.name === "task" ? displayedSubagents.get(row.card.callId) : undefined}
              />
            ) : row.kind === "web_search" ? (
              <WebSearchCard key={row.key} search={row.search} active={stream.isLoading} />
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
          ) : compactTurns.length > 0 ? (
            <div className="compact-conversation">
              {compactTurns.map((turn, index) => (
                <CompactTurnView
                  key={turn.userKey}
                  turn={turn}
                  active={stream.isLoading && index === compactTurns.length - 1}
                  apiUrl={apiUrl}
                  authHeaders={authHeaders}
                  threadId={threadId}
                  onStop={stopStream}
                />
              ))}
            </div>
          ) : null}

          {showDetails && visibleLiveWebSearches.length > 0 ? (
            <div className="web-search-live" aria-label="当前网页搜索">
              {visibleLiveWebSearches.map((search) => (
                <WebSearchCard key={search.item_id} search={search} active={stream.isLoading} />
              ))}
            </div>
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

          {currentQueueSize > 0 ? (
            <section className="queue-card" aria-labelledby="queue-title">
              <header>
                <div>
                  <p className="eyebrow">服务端队列</p>
                  <h2 id="queue-title">等待处理 · {currentQueueSize}</h2>
                </div>
                <button type="button" onClick={clearQueuedMessages}>
                  清空等待消息
                </button>
              </header>
              <ol>
                {queuedEntries.map((entry) => (
                  <li key={entry.id}>
                    <span>{queuedMessageText(entry.values)}</span>
                    <button
                      type="button"
                      onClick={() => cancelQueuedMessage(entry.id)}
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

        {showJumpToBottom && (showDetails ? rows.length > 0 : compactTurns.length > 0) ? (
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

        {authReady && providerConfigured === false ? (
          <section className="provider-required-card" role="status">
            <div>
              <strong>尚未配置模型 Provider</strong>
              <span>填写 API 地址、模型名和 Key 后即可发起研究任务。</span>
            </div>
            <button type="button" onClick={openSettings}>前往设置</button>
          </section>
        ) : null}

        <form className={`composer${showDetails ? "" : " composer--compact"}`} onSubmit={onSubmit}>
          <div
            className="composer__resize-divider"
            role="separator"
            aria-label="调整输入框高度"
            aria-orientation="horizontal"
            aria-valuemin={COMPOSER_TEXTAREA_MIN_HEIGHT}
            aria-valuemax={Math.round(composerMaxHeight())}
            aria-valuenow={Math.round(composerInputHeight)}
            tabIndex={0}
            onPointerDown={startComposerResize}
            onPointerMove={moveComposerResize}
            onPointerUp={finishComposerResize}
            onPointerCancel={finishComposerResize}
            onKeyDown={resizeComposerWithKeyboard}
          />
          <div className="composer__field">
            <label htmlFor="research-input">
              {pendingInterrupt
                ? "请先处理上方待确认事项"
                : workspaceLocked
                  ? "登录后可开始研究任务"
                : !providerReady
                  ? "配置模型 Provider 后可开始研究任务"
                : threadAllocating
                  ? "正在创建会话"
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
                disabled={workspaceLocked || identitySwitchBlocked}
                aria-label="选择本地表格文件"
              />
              <button
                type="button"
                className="attachment-button"
                disabled={workspaceLocked || identitySwitchBlocked || uploadedFiles.filter((file) => file.status === "ready").length >= MAX_UPLOAD_FILES}
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
              placeholder={workspaceLocked ? "请先登录或注册" : !providerReady ? "请先在设置中配置模型 Provider" : "例如：分析已上传订单表的月度趋势和异常值，并生成图表……"}
              disabled={workspaceLocked || !providerReady || pendingInterrupt !== null || threadAllocating}
            />
            <span>
              {pendingInterrupt
                ? "任务已暂停，提交决定后将从原位置继续"
                : stream.isLoading
                ? "Enter 排队发送 · Shift + Enter 换行"
                : "Enter 发送 · Shift + Enter 换行"}
            </span>
            <ContextWindowIndicator usage={contextUsage} />
          </div>
          <div className="composer__actions">
            <button
              className="send-button"
              type="submit"
              disabled={workspaceLocked || !providerReady || !input.trim() || pendingInterrupt !== null || !filesReadyForAnalysis || threadAllocating}
              aria-label={stream.isLoading ? "排队发送消息" : "发送分析任务"}
            >
              <span>{stream.isLoading ? "排队发送" : "发送任务"}</span>
              <i aria-hidden="true">↗</i>
            </button>
            {stream.isLoading && !pendingInterrupt ? (
              <button
                className="interrupt-button"
                type="button"
                disabled={!input.trim() || threadAllocating}
                onClick={() => void submitText(input, "interrupt")}
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
          <SubagentPlanPanel subagents={displayedSubagents} />
        </aside>
      ) : null}

      {limitDialog ? (
        <div className="limit-overlay" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget) setLimitDialog(null);
        }}>
          <section className="limit-dialog" role="dialog" aria-modal="true" aria-labelledby="limit-title">
            <header>
              <div>
                <p className="eyebrow">任务保护</p>
                <h2 id="limit-title">
                  {limitDialog.code === "THREAD_CONCURRENCY_LIMIT"
                    ? "运行中的会话已达上限"
                    : limitDialog.code === "QUESTION_RATE_LIMITED"
                      ? "本分钟提问次数已达上限"
                      : limitDialog.code === "TOKEN_BUDGET_EXHAUSTED"
                        ? "Token 额度不足"
                      : "暂时无法发起任务"}
                </h2>
              </div>
              <button type="button" onClick={() => setLimitDialog(null)} aria-label="关闭提示">×</button>
            </header>
            <p>{limitDialog.message}</p>
            {limitDialog.code === "QUESTION_RATE_LIMITED" || limitDialog.code === "TOKEN_BUDGET_EXHAUSTED" ? (
              <div className="limit-dialog__countdown" role="status">
                <strong>{limitCountdown}</strong>
                <span>秒后可重试</span>
              </div>
            ) : null}
            {limitDialog.code === "TOKEN_BUDGET_EXHAUSTED" ? (
              <div className="limit-dialog__token-summary">
                <span>当前余额</span>
                <strong>{(limitDialog.balance_tokens ?? 0).toLocaleString()} tokens</strong>
                <small>
                  每个整点补充 {(limitDialog.refill_tokens_per_hour ?? 0).toLocaleString()}
                  {limitDialog.next_refill_at
                    ? ` · 最早可用时间 ${new Date(limitDialog.next_refill_at).toLocaleString("zh-CN")}`
                    : ""}
                </small>
              </div>
            ) : null}
            {limitDialog.code === "THREAD_CONCURRENCY_LIMIT" && limitDialog.active_thread_ids?.length ? (
              <div className="limit-dialog__threads">
                <span>正在运行的会话</span>
                {limitDialog.active_thread_ids.map((activeThreadId) => {
                  const session = sessions.find((item) => item.thread_id === activeThreadId);
                  const title = typeof session?.metadata?.title === "string"
                    ? session.metadata.title
                    : "研究会话";
                  return (
                    <button
                      type="button"
                      key={activeThreadId}
                      onClick={() => {
                        setLimitDialog(null);
                        selectSession(activeThreadId);
                      }}
                    >
                      <strong>{title}</strong>
                      <small>{activeThreadId}</small>
                    </button>
                  );
                })}
              </div>
            ) : null}
            <footer>
              <button type="button" onClick={() => setLimitDialog(null)}>知道了</button>
            </footer>
          </section>
        </div>
      ) : null}

      {settingsOpen ? (
        <div className="settings-overlay" role="presentation" onMouseDown={(event) => {
          if (event.target === event.currentTarget) closeSettings();
        }}>
          <section className="settings-dialog" role="dialog" aria-modal="true" aria-labelledby="settings-title">
            <header>
              <div>
                <p className="eyebrow">个人工作台</p>
                <h2 id="settings-title">设置</h2>
              </div>
              <button
                ref={settingsCloseButtonRef}
                type="button"
                disabled={memoryClearing || memorySettingsUpdating || providerAction !== null}
                onClick={closeSettings}
                aria-label="关闭设置"
              >×</button>
            </header>

            <div className="settings-section">
              <div className="settings-section__copy">
                <strong>详细模式</strong>
                <span>显示工具调用、子智能体轨迹和完整执行计划。</span>
              </div>
              <button
                className="settings-toggle"
                type="button"
                role="switch"
                aria-label="详细模式"
                aria-checked={showDetails}
                onClick={() => setShowDetails((current) => !current)}
              >
                <i aria-hidden="true"><b /></i>
              </button>
            </div>

            <div className="settings-section settings-section--provider">
              <div className="settings-section__copy">
                <strong>模型 Provider</strong>
                <span>按当前账户加密保存。API Key 不会写入浏览器存储或任务消息。</span>
              </div>
              <div className="provider-settings-form">
                <label>
                  API Base URL
                  <input
                    type="url"
                    value={providerDraft.base_url}
                    disabled={!authReady || providerLoading || providerAction !== null || identitySwitchBlocked}
                    onChange={(event) => setProviderDraft((current) => ({
                      ...current,
                      base_url: event.target.value,
                    }))}
                    placeholder="https://api.example.com/v1"
                  />
                </label>
                <label>
                  模型名
                  <input
                    value={providerDraft.model_name}
                    disabled={!authReady || providerLoading || providerAction !== null || identitySwitchBlocked}
                    onChange={(event) => setProviderDraft((current) => ({
                      ...current,
                      model_name: event.target.value,
                    }))}
                    placeholder="qwen-plus"
                  />
                </label>
                <label>
                  API Key
                  <span className="provider-key-field">
                    <input
                      type={providerKeyVisible ? "text" : "password"}
                      autoComplete="new-password"
                      value={providerApiKey}
                      disabled={!authReady || providerLoading || providerAction !== null || identitySwitchBlocked}
                      onChange={(event) => setProviderApiKey(event.target.value)}
                      placeholder={providerConfigured ? `已保存 · 尾号 ${providerKeyHint || "****"}，留空则保留` : "请输入 API Key"}
                    />
                    <button
                      type="button"
                      disabled={!providerApiKey}
                      onClick={() => setProviderKeyVisible((current) => !current)}
                      aria-label={providerKeyVisible ? "隐藏 API Key" : "显示 API Key"}
                    >{providerKeyVisible ? "隐藏" : "显示"}</button>
                  </span>
                </label>
                {providerIdentity ? (
                  <p className="settings-hint">
                    已识别：{PROVIDER_TYPE_LABELS[providerIdentity.provider_type]} · {providerIdentity.provider_name}
                  </p>
                ) : null}
                <div className="provider-settings-actions">
                  <button
                    type="button"
                    disabled={!authReady || providerLoading || providerAction !== null}
                    onClick={() => void testModelProvider()}
                  >{providerAction === "testing" ? "测试中…" : "测试连接"}</button>
                  <button
                    type="button"
                    disabled={!authReady || providerLoading || providerAction !== null || identitySwitchBlocked}
                    onClick={() => void saveModelProvider()}
                  >{providerAction === "saving" ? "保存中…" : "保存 Provider"}</button>
                  {providerConfigured ? (
                    <button
                      type="button"
                      className="provider-settings-delete"
                      disabled={!authReady || providerAction !== null || identitySwitchBlocked}
                      onClick={() => void deleteModelProvider()}
                    >{providerAction === "deleting" ? "删除中…" : "删除"}</button>
                  ) : null}
                </div>
                {providerLoading ? <p className="settings-hint">正在读取 Provider 配置…</p> : null}
                {identitySwitchBlocked ? <p className="settings-hint">当前任务结束后才能修改 Provider。</p> : null}
                {providerStatus ? (
                  <p className={`settings-status settings-status--${providerStatus.tone}`} role="status">
                    {providerStatus.message}
                  </p>
                ) : null}
              </div>
            </div>

            <div className="settings-section">
              <div className="settings-section__copy">
                <strong>失败经验整理</strong>
                <span>
                  {failureLessonSavingEnabled === false
                    ? "仍会使用已有公共经验，但不会根据你的任务新增经验。"
                    : "允许系统根据工具执行结果后台整理可复用的失败教训。"}
                </span>
              </div>
              <button
                className="settings-toggle"
                type="button"
                role="switch"
                aria-label="失败经验整理"
                aria-checked={failureLessonSavingEnabled === true}
                disabled={!authReady || memorySettingsLoading || memorySettingsUpdating || failureLessonSavingEnabled === null}
                onClick={() => void updateFailureLessonSaving()}
              >
                <i aria-hidden="true"><b /></i>
              </button>
              {!authReady ? <p className="settings-hint">登录后才能修改失败经验设置。</p> : null}
              {authReady && memorySettingsLoading ? <p className="settings-hint">正在读取设置…</p> : null}
              {memorySettingsError ? (
                <p className="settings-status settings-status--error" role="status">{memorySettingsError}</p>
              ) : null}
            </div>

            <div className="settings-section settings-section--danger">
              <div className="settings-section__copy">
                <strong>清除记忆</strong>
                <span>清除当前用户的偏好和行为反馈；会话、文件、Skill 与公共失败经验不受影响。</span>
              </div>
              {!memoryClearConfirm ? (
                <button
                  className="settings-danger-button"
                  type="button"
                  disabled={!authReady || identitySwitchBlocked || memoryClearing}
                  onClick={() => {
                    setMemoryClearConfirm(true);
                    setMemoryStatus(null);
                  }}
                >清除记忆</button>
              ) : (
                <div className="settings-confirm" role="alert">
                  <p>确认清除已记录的偏好和行为反馈？此操作无法撤销。</p>
                  <div>
                    <button type="button" disabled={memoryClearing} onClick={() => setMemoryClearConfirm(false)}>取消</button>
                    <button type="button" disabled={memoryClearing} onClick={() => void clearUserMemory()}>
                      {memoryClearing ? "正在清除…" : "确认清除"}
                    </button>
                  </div>
                </div>
              )}
              {!authReady ? <p className="settings-hint">登录后才能清除用户记忆。</p> : null}
              {authReady && identitySwitchBlocked ? <p className="settings-hint">当前任务结束后才能清除记忆。</p> : null}
              {memoryStatus ? (
                <p className={`settings-status settings-status--${memoryStatus.tone}`} role="status">
                  {memoryStatus.message}
                </p>
              ) : null}
            </div>
          </section>
        </div>
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
