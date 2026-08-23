import type { ClassSubagentStreamInterface } from "@langchain/react";
import { useState } from "react";

export type SubagentTraceStream = ClassSubagentStreamInterface<Record<string, unknown>>;

const STATUS_LABEL = {
  pending: "等待启动",
  running: "执行中",
  complete: "已完成",
  error: "执行失败",
} as const;

const TOOL_STATUS_LABEL = {
  pending: "执行中",
  completed: "已完成",
  error: "失败",
} as const;

const PREVIEW_LIMIT = 4_000;
const MAX_PROGRESS_MESSAGES = 12;
const MAX_TOOL_CALLS = 30;

type MessageLike = {
  content?: unknown;
  type?: string;
  getType?: () => string;
  _getType?: () => string;
};

type SubagentToolExecution = SubagentTraceStream["toolCalls"][number];

function preview(value: string): string {
  if (value.length <= PREVIEW_LIMIT) return value;
  return `${value.slice(0, PREVIEW_LIMIT)}\n\n…内容过长，仅显示前 ${PREVIEW_LIMIT} 个字符。`;
}

function formatValue(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function visibleMessageText(content: unknown): string {
  if (typeof content === "string") return content;
  if (!Array.isArray(content)) return "";

  // 只展示正常文本块，不读取 reasoning_content 或其他隐藏推理字段。
  return content
    .map((block) => {
      if (!block || typeof block !== "object") return "";
      const candidate = block as { type?: unknown; text?: unknown };
      if (candidate.type !== undefined && candidate.type !== "text") return "";
      return typeof candidate.text === "string" ? candidate.text : "";
    })
    .filter(Boolean)
    .join("\n");
}

function messageType(message: MessageLike): string {
  if (typeof message.type === "string") return message.type;
  if (typeof message.getType === "function") return message.getType();
  if (typeof message._getType === "function") return message._getType();
  return "";
}

function timeValue(value: Date | string | null): number | null {
  if (!value) return null;
  const timestamp = value instanceof Date ? value.getTime() : new Date(value).getTime();
  return Number.isFinite(timestamp) ? timestamp : null;
}

function durationLabel(subagent: SubagentTraceStream): string | null {
  const startedAt = timeValue(subagent.startedAt);
  if (startedAt === null) return null;
  const completedAt = timeValue(subagent.completedAt);
  const elapsedSeconds = Math.max(0, Math.round(((completedAt ?? Date.now()) - startedAt) / 1_000));
  if (elapsedSeconds < 60) return `${elapsedSeconds} 秒`;
  return `${Math.floor(elapsedSeconds / 60)} 分 ${elapsedSeconds % 60} 秒`;
}

function ToolExecution({ toolCall }: { toolCall: SubagentToolExecution }) {
  // 即使首次收到时已经完成也保持展开，后续只由用户决定是否折叠。
  const [open, setOpen] = useState(true);
  const result = toolCall.result
    ? visibleMessageText((toolCall.result as unknown as MessageLike).content)
    : "";

  return (
    <li className={`is-${toolCall.state}`}>
      <details
        open={open}
        onToggle={(event) => setOpen((event.target as HTMLDetailsElement).open)}
      >
        <summary>
          <code>{toolCall.call.name}</code>
          <span>{TOOL_STATUS_LABEL[toolCall.state]}</span>
        </summary>
        <div>
          <small>输入</small>
          <pre>{preview(formatValue(toolCall.call.args))}</pre>
          {result ? (
            <>
              <small>结果</small>
              <pre>{preview(result)}</pre>
            </>
          ) : null}
        </div>
      </details>
    </li>
  );
}

export default function SubagentTrace({ subagent }: { subagent: SubagentTraceStream }) {
  const agentName = subagent.toolCall.args.subagent_type ?? "subagent";
  const progressMessages = subagent.messages
    .filter((message) => messageType(message as unknown as MessageLike) === "ai")
    .map((message) => visibleMessageText((message as unknown as MessageLike).content).trim())
    .filter((content) => content && content !== subagent.result)
    .slice(-MAX_PROGRESS_MESSAGES);
  const toolCalls = subagent.toolCalls.slice(-MAX_TOOL_CALLS);
  const duration = durationLabel(subagent);
  const errorText = subagent.error ? String(subagent.error) : "";

  return (
    <section
      className={`subagent-trace subagent-trace--${subagent.status}`}
      aria-label={`${agentName} 子智能体执行过程`}
    >
      <header className="subagent-trace__header">
        <div>
          <span className="subagent-trace__node" aria-hidden="true">↳</span>
          <div>
            <small>同步子智能体</small>
            <strong>{agentName}</strong>
          </div>
        </div>
        <div className="subagent-trace__meta">
          {duration ? <span>{duration}</span> : null}
          <b>{STATUS_LABEL[subagent.status]}</b>
        </div>
      </header>

      {progressMessages.length > 0 ? (
        <div className="subagent-trace__section">
          <span className="subagent-trace__label">可见进展</span>
          <ol className="subagent-progress">
            {progressMessages.map((content, index) => (
              <li key={`${index}-${content.slice(0, 40)}`}>{preview(content)}</li>
            ))}
          </ol>
        </div>
      ) : null}

      {toolCalls.length > 0 ? (
        <div className="subagent-trace__section">
          <span className="subagent-trace__label">工具执行 · {subagent.toolCalls.length}</span>
          <ol className="subagent-tools">
            {toolCalls.map((toolCall) => (
              <ToolExecution key={toolCall.id} toolCall={toolCall} />
            ))}
          </ol>
        </div>
      ) : null}

      {subagent.status === "pending" && toolCalls.length === 0 ? (
        <p className="subagent-trace__empty">已接收委派，正在等待子图事件。</p>
      ) : null}

      {errorText ? (
        <div className="subagent-trace__result is-error">
          <span className="subagent-trace__label">错误</span>
          <pre>{preview(errorText)}</pre>
        </div>
      ) : subagent.result ? (
        <div className="subagent-trace__result">
          <span className="subagent-trace__label">最终返回</span>
          <pre>{preview(subagent.result)}</pre>
        </div>
      ) : null}
    </section>
  );
}
