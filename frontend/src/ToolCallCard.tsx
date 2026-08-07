import { memo, useMemo, useState } from "react";
import SubagentTrace, { type SubagentTraceStream } from "./SubagentTrace";

export type ToolCard = {
  callId: string;
  name: string;
  args: unknown;
  result: string | null;
  status: "pending" | "done";
};

const TOOL_LABELS: Record<string, string> = {
  write_todos: "更新研究计划",
  task: "调用同步子智能体",
  start_async_task: "启动后台任务",
  check_async_task: "检查任务进度",
  update_async_task: "补充任务要求",
  cancel_async_task: "取消采集任务",
  list_async_tasks: "读取任务列表",
  assign_skill: "分配 Skill",
  ask_user: "请求补充信息",
  request_report_download: "准备报告下载",
  write_file: "写入研究产物",
  read_file: "读取研究产物",
  edit_file: "编辑研究产物",
};

const TOOL_PREVIEW_LIMIT = 8_000;

function formatValue(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function previewValue(value: string): string {
  if (value.length <= TOOL_PREVIEW_LIMIT) return value;
  return `${value.slice(0, TOOL_PREVIEW_LIMIT)}\n\n…内容过长，仅显示前 ${TOOL_PREVIEW_LIMIT} 个字符。`;
}

function ToolCallCard({ card, subagent }: { card: ToolCard; subagent?: SubagentTraceStream }) {
  // 同步子智能体卡始终首次展开；状态更新不再改变用户选择的开合状态。
  const [open, setOpen] = useState(card.name === "task" || card.status === "pending");
  // 折叠时不序列化或挂载大结果，避免每个流式 token 都处理完整网页正文。
  const argsPreview = useMemo(
    () => (open ? previewValue(formatValue(card.args)) : ""),
    [card.args, open],
  );
  const resultPreview = useMemo(
    () => (open && card.result !== null ? previewValue(card.result) : null),
    [card.result, open],
  );

  return (
    <details
      className={`tool-card tool-card--${card.status}`}
      open={open}
      onToggle={(event) => setOpen((event.target as HTMLDetailsElement).open)}
    >
      <summary>
        <span className="tool-card__glyph" aria-hidden="true">⌁</span>
        <span className="tool-card__name">{TOOL_LABELS[card.name] ?? card.name}</span>
        <span className="tool-card__status">
          {card.status === "pending" ? "执行中" : "已完成"}
        </span>
      </summary>
      {open ? (
        <div className="tool-card__details">
          <div>
            <span className="tool-card__label">输入</span>
            <pre>{argsPreview}</pre>
          </div>
          {subagent ? <SubagentTrace subagent={subagent} /> : null}
          {resultPreview !== null ? (
            <div>
              <span className="tool-card__label">结果</span>
              <pre>{resultPreview}</pre>
            </div>
          ) : null}
        </div>
      ) : null}
    </details>
  );
}


export default memo(ToolCallCard, (previous, next) => (
  previous.card.callId === next.card.callId
  && previous.card.status === next.card.status
  && previous.card.args === next.card.args
  && previous.card.result === next.card.result
  && previous.subagent === next.subagent
));
