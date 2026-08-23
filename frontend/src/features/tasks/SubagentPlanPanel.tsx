import { useRef, useState } from "react";
import type { SubagentTraceStream } from "../traces/SubagentTrace";
import type { TodoItem, TodoStatus } from "./TodoPanel";

const STATUS_LABEL = {
  pending: "等待启动",
  running: "执行中",
  complete: "已完成",
  error: "执行失败",
} as const;

const TODO_STATUS_LABEL: Record<TodoStatus, string> = {
  pending: "等待",
  in_progress: "进行中",
  completed: "完成",
};

function parseTodoList(value: unknown): TodoItem[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is TodoItem => {
    if (!item || typeof item !== "object") return false;
    const todo = item as Partial<TodoItem>;
    return typeof todo.content === "string"
      && (todo.status === "pending" || todo.status === "in_progress" || todo.status === "completed");
  });
}

function latestTodos(subagent: SubagentTraceStream): TodoItem[] {
  const stateTodos = parseTodoList(subagent.values.todos);
  if (stateTodos.length > 0) return stateTodos;

  // 同步子图完成后最终 values 可能不再携带临时 todo 状态；从最后一次
  // write_todos 调用恢复，保留用户刚才看到的执行计划。
  for (let index = subagent.toolCalls.length - 1; index >= 0; index -= 1) {
    const call = subagent.toolCalls[index]?.call;
    if (call?.name !== "write_todos" || !call.args || typeof call.args !== "object") continue;
    const todos = parseTodoList((call.args as { todos?: unknown }).todos);
    if (todos.length > 0) return todos;
  }
  return [];
}

function SubagentPlanCard({
  subagent,
  order,
}: {
  subagent: SubagentTraceStream;
  order: number;
}) {
  // 默认展开；后续流式更新不改变用户手动选择的折叠状态。
  const [open, setOpen] = useState(true);
  const cachedTodos = useRef<TodoItem[]>([]);
  const agentName = subagent.toolCall.args.subagent_type ?? "subagent";
  const observedTodos = latestTodos(subagent);
  if (observedTodos.length > 0) cachedTodos.current = observedTodos;
  const todos = observedTodos.length > 0 ? observedTodos : cachedTodos.current;
  const completed = todos.filter((todo) => todo.status === "completed").length;

  return (
    <details
      className={`subagent-plan-panel is-${subagent.status}`}
      open={open}
      onToggle={(event) => setOpen((event.currentTarget as HTMLDetailsElement).open)}
      aria-label={`${agentName} 子智能体计划 · 调用 ${order}`}
    >
      <summary>
        <div>
          <small>同步执行 · #{order}</small>
          <strong>{agentName}</strong>
        </div>
        <div className="subagent-plan-panel__meta">
          <span>{STATUS_LABEL[subagent.status]}</span>
          <i aria-hidden="true">⌄</i>
        </div>
      </summary>

      <div className="subagent-plan-panel__body">
        <div className="subagent-plan-panel__title">
          <span>执行计划</span>
          {todos.length > 0 ? <b>{completed}/{todos.length}</b> : null}
        </div>

        {todos.length === 0 ? (
          <p className="subagent-plan-panel__empty">
            {subagent.status === "complete"
              ? "本次调用未生成可展示的执行计划。"
              : "已接收委派，等待生成执行计划。"}
          </p>
        ) : (
          <ol>
            {todos.map((todo, index) => (
              <li key={`${todo.content}-${index}`} className={`is-${todo.status}`}>
                <i aria-hidden="true" />
                <div>
                  <span>{todo.content}</span>
                  <small>{TODO_STATUS_LABEL[todo.status]}</small>
                </div>
              </li>
            ))}
          </ol>
        )}
      </div>
    </details>
  );
}

export default function SubagentPlanPanel({
  subagents,
}: {
  subagents: Map<string, SubagentTraceStream>;
}) {
  const candidates = Array.from(subagents.values())
    .map((subagent, index) => ({ subagent, order: index + 1 }))
    .reverse();
  if (candidates.length === 0) return null;

  return (
    <section className="side-section subagent-plan-section" aria-label="子智能体计划">
      <div className="side-section__heading">
        <div>
          <p className="eyebrow">同步委派</p>
          <h2>子智能体计划</h2>
        </div>
        <span className="trace-count">{candidates.length}</span>
      </div>

      <div className="subagent-plan-list">
        {candidates.map(({ subagent, order }) => (
          <SubagentPlanCard key={subagent.id} subagent={subagent} order={order} />
        ))}
      </div>
    </section>
  );
}
