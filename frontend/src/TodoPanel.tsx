export type TodoStatus = "pending" | "in_progress" | "completed";

export type TodoItem = {
  content: string;
  status: TodoStatus;
};

const STATUS_LABEL: Record<TodoStatus, string> = {
  pending: "等待",
  in_progress: "进行中",
  completed: "完成",
};

export default function TodoPanel({ todos }: { todos: TodoItem[] }) {
  const completed = todos.filter((todo) => todo.status === "completed").length;
  const progress = todos.length === 0 ? 0 : Math.round((completed / todos.length) * 100);

  return (
    <section className="side-section" aria-labelledby="plan-title">
      <div className="side-section__heading">
        <div>
          <p className="eyebrow">当前计划</p>
          <h2 id="plan-title">研究步骤</h2>
        </div>
        <span className="plan-progress">{progress}%</span>
      </div>

      {todos.length === 0 ? (
        <p className="side-empty">发送任务后，这里会显示 Agent 的实时计划。</p>
      ) : (
        <ol className="todo-list">
          {todos.map((todo, index) => (
            <li
              key={`${todo.content}-${index}`}
              className={`todo-item todo-item--${todo.status}`}
            >
              <span className="todo-item__marker" aria-hidden="true" />
              <div>
                <span className="todo-item__content">{todo.content}</span>
                <span className="todo-item__status">{STATUS_LABEL[todo.status]}</span>
              </div>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
