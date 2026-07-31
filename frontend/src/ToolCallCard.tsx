import { useEffect, useRef, useState } from "react";

export type ToolCard = {
  callId: string;
  name: string;
  args: unknown;
  result: string | null;
  status: "pending" | "done";
};

const TOOL_LABELS: Record<string, string> = {
  write_todos: "更新研究计划",
  start_async_task: "启动后台任务",
  check_async_task: "检查任务进度",
  update_async_task: "补充任务要求",
  cancel_async_task: "取消采集任务",
  list_async_tasks: "读取任务列表",
  assign_skill: "分配 Skill",
  write_file: "写入研究产物",
  read_file: "读取研究产物",
  edit_file: "编辑研究产物",
};

function formatValue(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

export default function ToolCallCard({ card }: { card: ToolCard }) {
  const [open, setOpen] = useState(card.status === "pending");
  const previousStatus = useRef(card.status);

  useEffect(() => {
    if (previousStatus.current === "pending" && card.status === "done") {
      setOpen(false);
    }
    previousStatus.current = card.status;
  }, [card.status]);

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
      <div className="tool-card__details">
        <div>
          <span className="tool-card__label">输入</span>
          <pre>{formatValue(card.args)}</pre>
        </div>
        {card.result !== null ? (
          <div>
            <span className="tool-card__label">结果</span>
            <pre>{card.result}</pre>
          </div>
        ) : null}
      </div>
    </details>
  );
}
