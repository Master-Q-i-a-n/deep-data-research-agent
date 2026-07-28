import { FormEvent, useState } from "react";

export type AsyncTaskStatus =
  | "pending"
  | "running"
  | "success"
  | "error"
  | "cancelled"
  | "timeout"
  | "interrupted";

export type AsyncTask = {
  task_id: string;
  agent_name?: string;
  status: AsyncTaskStatus;
  created_at?: string;
  last_checked_at?: string;
  last_updated_at?: string;
};

const STATUS_LABEL: Record<AsyncTaskStatus, string> = {
  pending: "等待启动",
  running: "采集中",
  success: "已完成",
  error: "失败",
  cancelled: "已取消",
  timeout: "已超时",
  interrupted: "已中断",
};

function shortId(value: string): string {
  return value.length > 14 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

function formatCheckedAt(value?: string): string {
  if (!value) return "尚未检查";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return `上次检查 ${date.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  })}`;
}

type TaskTraceProps = {
  tasks: AsyncTask[];
  onCheck: (taskId: string) => void;
  onUpdate: (taskId: string, message: string) => void;
  onCancel: (taskId: string) => void;
  onRefresh: () => void;
};

export default function TaskTrace({
  tasks,
  onCheck,
  onUpdate,
  onCancel,
  onRefresh,
}: TaskTraceProps) {
  const [editingTaskId, setEditingTaskId] = useState<string | null>(null);
  const [updateText, setUpdateText] = useState("");

  function submitUpdate(event: FormEvent, taskId: string) {
    event.preventDefault();
    const message = updateText.trim();
    if (!message) return;
    onUpdate(taskId, message);
    setEditingTaskId(null);
    setUpdateText("");
  }

  function toggleUpdate(taskId: string) {
    setEditingTaskId((current) => (current === taskId ? null : taskId));
    setUpdateText("");
  }

  return (
    <section
      className={`side-section trace-section${tasks.length === 0 ? " trace-section--empty" : ""}`}
      aria-labelledby="trace-title"
    >
      <div className="side-section__heading">
        <div>
          <p className="eyebrow">采集轨迹</p>
          <h2 id="trace-title">任务控制台</h2>
        </div>
        <div className="trace-heading__actions">
          <span className="trace-count">{tasks.length}</span>
          <button
            type="button"
            className="trace-refresh"
            onClick={onRefresh}
            disabled={tasks.length === 0}
          >
            刷新
          </button>
        </div>
      </div>

      {tasks.length === 0 ? (
        <div className="trace-empty">
          <span className="trace-empty__node" aria-hidden="true" />
          <p>还没有网页采集任务。</p>
        </div>
      ) : (
        <ol className="trace-list">
          {tasks.map((task) => (
            <li key={task.task_id} className={`trace-item trace-item--${task.status}`}>
              <span className="trace-item__node" aria-hidden="true" />
              <div className="trace-item__body">
                <div className="trace-item__title">
                  <strong>{task.agent_name ?? "crawl-worker"}</strong>
                  <span>{STATUS_LABEL[task.status]}</span>
                </div>
                <code title={task.task_id}>{shortId(task.task_id)}</code>
                <small>{formatCheckedAt(task.last_checked_at)}</small>

                <div className="trace-item__actions">
                  {task.status !== "cancelled" ? (
                    <button type="button" onClick={() => onCheck(task.task_id)}>
                      {task.status === "success" ? "读取结果" : "检查进度"}
                    </button>
                  ) : null}
                  {task.status === "running" || task.status === "pending" ? (
                    <>
                      <button type="button" onClick={() => toggleUpdate(task.task_id)}>
                        补充要求
                      </button>
                      <button
                        type="button"
                        className="trace-item__cancel"
                        onClick={() => onCancel(task.task_id)}
                      >
                        取消
                      </button>
                    </>
                  ) : null}
                </div>

                {editingTaskId === task.task_id ? (
                  <form
                    className="task-update"
                    onSubmit={(event) => submitUpdate(event, task.task_id)}
                  >
                    <label htmlFor={`task-update-${task.task_id}`}>补充采集要求</label>
                    <textarea
                      id={`task-update-${task.task_id}`}
                      rows={3}
                      value={updateText}
                      onChange={(event) => setUpdateText(event.target.value)}
                      placeholder="例如：只保留国内数据，并增加价格字段"
                      autoFocus
                    />
                    <div>
                      <button
                        type="button"
                        onClick={() => {
                          setEditingTaskId(null);
                          setUpdateText("");
                        }}
                      >
                        取消
                      </button>
                      <button type="submit" disabled={!updateText.trim()}>
                        立即更新
                      </button>
                    </div>
                  </form>
                ) : null}
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
